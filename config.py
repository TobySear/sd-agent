# (C) Toby Sear 2022
# (C) Server Density 2009-2017
# (C) Datadog, Inc. 2010-2016
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)

# stdlib
import ConfigParser
from cStringIO import StringIO
import glob
import imp
import inspect
import itertools
import logging
import logging.config
import logging.handlers
from optparse import OptionParser, Values
import os
import platform
import re
from socket import gaierror, gethostbyname
import string
import sys
import traceback
from importlib import import_module

# 3p
import simplejson as json

# project
from util import check_yaml, config_to_yaml
from utils.platform import Platform, get_os
from utils.proxy import get_proxy
from utils.sdk import load_manifest
from utils.service_discovery.config import extract_agent_config
from utils.service_discovery.config_stores import CONFIG_FROM_FILE, TRACE_CONFIG
from utils.service_discovery.sd_backend import get_sd_backend, AUTO_CONFIG_DIR, SD_BACKENDS
from utils.subprocess_output import (
    get_subprocess_output,
    SubprocessOutputEmptyError,
)
from utils.windows_configuration import get_registry_conf, get_windows_sdk_check


# CONSTANTS
AGENT_VERSION = "2.2.10"
JMX_VERSION = "0.44.6"
SD_CONF = "config.cfg"
UNIX_CONFIG_PATH = '/etc/sd-agent'
MAC_CONFIG_PATH = '/usr/local/etc/sd-agent/'
DEFAULT_CHECK_FREQUENCY = 60   # seconds
LOGGING_MAX_BYTES = 10 * 1024 * 1024
SDK_INTEGRATIONS_DIR = 'integrations'
SD_PIPE_NAME = "sd-service_discovery"
SD_PIPE_UNIX_PATH = '/var/run/sd-agent/'
SD_PIPE_WIN_PATH = "\\\\.\\pipe\\{pipename}"

log = logging.getLogger(__name__)

OLD_STYLE_PARAMETERS = [
    ('apache_status_url', "apache"),
    ('cacti_mysql_server', "cacti"),
    ('couchdb_server', "couchdb"),
    ('elasticsearch', "elasticsearch"),
    ('haproxy_url', "haproxy"),
    ('hudson_home', "Jenkins"),
    ('memcache_', "memcached"),
    ('mongodb_server', "mongodb"),
    ('mysql_server', "mysql"),
    ('nginx_status_url', "nginx"),
    ('postgresql_server', "postgres"),
    ('redis_urls', "redis"),
    ('varnishstat', "varnish"),
    ('WMI', "WMI"),
]

NAGIOS_OLD_CONF_KEYS = [
    'nagios_log',
    'nagios_perf_cfg'
]


JMX_SD_CONF_TEMPLATE = '.jmx.{}.yaml'

# These are unlikely to change, but manifests are versioned,
# so keeping these as a list just in case we change add stuff.
MANIFEST_VALIDATION = {
    'max': ['max_agent_version'],
    'min': ['min_agent_version']
}


class PathNotFound(Exception):
    pass

class ApiKeyNotFound(Exception):
    pass

class ApiKeyInvalid(Exception):
    pass

def get_parsed_args():
    parser = OptionParser()
    parser.add_option('-A', '--autorestart', action='store_true', default=False,
                      dest='autorestart')
    parser.add_option('-s', '--sd_url', action='store', default=None,
                      dest='sd_url')
    parser.add_option('-u', '--use-local-forwarder', action='store_true',
                      default=False, dest='use_forwarder')
    parser.add_option('-v', '--verbose', action='store_true', default=False,
                      dest='verbose',
                      help='Print out stacktraces for errors in checks')
    parser.add_option('-p', '--profile', action='store_true', default=False,
                      dest='profile', help='Enable Developer Mode')

    try:
        options, args = parser.parse_args()
    except SystemExit:
        # Ignore parse errors
        options, args = Values({'autorestart': False,
                                'sd_url': None,
                                'use_forwarder': False,
                                'verbose': False,
                                'profile': False}), []
    return options, args


def get_version():
    return AGENT_VERSION


def _version_string_to_tuple(version_string):
    '''Return a (X, Y, Z) version tuple from an 'X.Y.Z' version string'''
    version_list = []
    for elem in version_string.split('.'):
        try:
            elem_int = int(elem)
        except ValueError:
            log.warning("Unable to parse element '%s' of version string '%s'", elem, version_string)
            raise

        version_list.append(elem_int)

    return tuple(version_list)


# Return url endpoint, here because needs access to version number
def get_url_endpoint(default_url, endpoint_type='app', cfg_path=None):
    config_path = get_config_path(cfg_path, os_name=get_os())
    config = ConfigParser.ConfigParser()
    config.readfp(skip_leading_wsp(open(config_path)))
    if config.has_option('Main', 'sd_url') and config.get('Main', 'sd_url'):
        url = config.get('Main', 'sd_url')
    elif config.has_option('Main', 'sd_account') and config.get('Main', 'sd_account'):
        url = 'https://{}.local'.format(config.get('Main', 'sd_account'))
    else:
        # Default agent URL
        url = default_url
    if url.endswith('/'):
        url = url[:-1]
    return url


def skip_leading_wsp(f):
    "Works on a file, returns a file-like object"
    return StringIO("\n".join(map(string.strip, f.readlines())))


def _windows_commondata_path():
    """Return the common appdata path, using ctypes
    From http://stackoverflow.com/questions/626796/\
    how-do-i-find-the-windows-common-application-data-folder-using-python
    """
    import ctypes
    from ctypes import wintypes, windll

    CSIDL_COMMON_APPDATA = 35

    _SHGetFolderPath = windll.shell32.SHGetFolderPathW
    _SHGetFolderPath.argtypes = [wintypes.HWND,
                                 ctypes.c_int,
                                 wintypes.HANDLE,
                                 wintypes.DWORD, wintypes.LPCWSTR]

    path_buf = wintypes.create_unicode_buffer(wintypes.MAX_PATH)
    _SHGetFolderPath(0, CSIDL_COMMON_APPDATA, 0, 0, path_buf)
    return path_buf.value


def _windows_extra_checksd_path():
    common_data = _windows_commondata_path()
    return os.path.join(common_data, 'Server Density', 'checks.d')


def _windows_checksd_path():
    if hasattr(sys, 'frozen'):
        # we're frozen - from py2exe
        prog_path = os.path.dirname(sys.executable)
        return _checksd_path(os.path.normpath(os.path.join(prog_path, '..', 'agent')))
    else:
        cur_path = os.path.dirname(__file__)
        return _checksd_path(cur_path)


def _config_path(directory):
    path = os.path.join(directory, SD_CONF)
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _confd_path(directory):
    path = os.path.join(directory, 'conf.d')
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _checksd_path(directory):
    path_override = os.environ.get('CHECKSD_OVERRIDE')
    if path_override and os.path.exists(path_override):
        return path_override

    # this is deprecated in testing on versions after SDK (5.12.0)
    path = os.path.join(directory, 'checks.d')
    if os.path.exists(path):
        return path
    raise PathNotFound(path)


def _is_affirmative(s):
    if s is None:
        return False
    # int or real bool
    if isinstance(s, int):
        return bool(s)
    # try string cast
    return s.lower() in ('yes', 'true', '1')


def get_config_path(cfg_path=None, os_name=None):
    # Check if there's an override and if it exists
    if cfg_path is not None and os.path.exists(cfg_path):
        return cfg_path

    # Check if there's a config stored in the current agent directory
    try:
        path = os.path.realpath(__file__)
        path = os.path.dirname(path)
        return _config_path(path)
    except PathNotFound as e:
        pass

    # Check for an OS-specific path, continue on not-found exceptions
    bad_path = ''
    try:
        if Platform.is_windows():
            common_data = _windows_commondata_path()
            return _config_path(os.path.join(common_data, 'Server Density'))
        elif Platform.is_mac():
            return _config_path(MAC_CONFIG_PATH)
        else:
            return _config_path(UNIX_CONFIG_PATH)
    except PathNotFound as e:
        if len(e.args) > 0:
            bad_path = e.args[0]

    # If all searches fail, exit the agent with an error
    sys.stderr.write("Please supply a configuration file at %s or in the directory where "
                     "the Agent is currently deployed.\n" % bad_path)
    sys.exit(3)


def get_default_bind_host():
    try:
        gethostbyname('localhost')
    except gaierror:
        log.warning("localhost seems undefined in your hosts file, using 127.0.0.1 instead")
        return '127.0.0.1'
    return 'localhost'


def get_histogram_aggregates(configstr=None):
    if configstr is None:
        return None

    try:
        vals = configstr.split(',')
        valid_values = ['min', 'max', 'median', 'avg', 'sum', 'count']
        result = []

        for val in vals:
            val = val.strip()
            if val not in valid_values:
                log.warning("Ignored histogram aggregate {0}, invalid".format(val))
                continue
            else:
                result.append(val)
    except Exception:
        log.exception("Error when parsing histogram aggregates, skipping")
        return None

    return result


def get_histogram_percentiles(configstr=None):
    if configstr is None:
        return None

    result = []
    try:
        vals = configstr.split(',')
        for val in vals:
            try:
                val = val.strip()
                floatval = float(val)
                if floatval <= 0 or floatval >= 1:
                    raise ValueError
                if len(val) > 4:
                    log.warning("Histogram percentiles are rounded to 2 digits: {0} rounded"
                                .format(floatval))
                result.append(float(val[0:4]))
            except ValueError:
                log.warning("Bad histogram percentile value {0}, must be float in ]0;1[, skipping"
                            .format(val))
    except Exception:
        log.exception("Error when parsing histogram percentiles, skipping")
        return None

    return result


def clean_dd_url(url):
    url = url.strip()
    if not url.startswith('http'):
        url = 'https://' + url
    return url[:-1] if url.endswith('/') else url


def remove_empty(string_array):
    return filter(lambda x: x, string_array)


def get_config(parse_args=True, cfg_path=None, options=None, can_query_registry=True, allow_invalid_api_key=False):
    if parse_args:
        options, _ = get_parsed_args()

    # General config
    agentConfig = {
        'check_freq': DEFAULT_CHECK_FREQUENCY,
        'collect_orchestrator_tags': True,
        'sdstatsd_port': 8125,
        'sdstatsd_target': 'http://localhost:17124',
        'graphite_listen_port': None,
        'hostname': None,
        'listen_port': None,
        'tags': None,
        'use_ec2_instance_id': False,  # DEPRECATED
        'version': get_version(),
        'watchdog': True,
        'additional_checksd': '/etc/sd-agent/checks.d/',
        'bind_host': get_default_bind_host(),
        'statsd_metric_namespace': None,
        'utf8_decoding': False,
        'apm_enabled': False
    }

    if Platform.is_mac():
        agentConfig['additional_checksd'] = '/usr/local/etc/sd-agent/checks.d/'
    elif Platform.is_windows():
        agentConfig['additional_checksd'] = _windows_extra_checksd_path()

    # Config handling
    try:
        # Find the right config file
        path = os.path.realpath(__file__)
        path = os.path.dirname(path)

        config_path = get_config_path(cfg_path, os_name=get_os())
        config = ConfigParser.ConfigParser()
        config.readfp(skip_leading_wsp(open(config_path)))

        # bulk import
        for option in config.options('Main'):
            agentConfig[option] = config.get('Main', option)

        # Store developer mode setting in the agentConfig
        if config.has_option('Main', 'developer_mode'):
            agentConfig['developer_mode'] = _is_affirmative(config.get('Main', 'developer_mode'))

        # Allow an override with the --profile option
        if options is not None and options.profile:
            agentConfig['developer_mode'] = True

        # Core config
        #ap
        if not config.has_option('Main', 'agent_key'):
            log.warning(u"No agent key was found. Aborting.")
            sys.exit(2)

        if not (config.has_option('Main', 'sd_url') or config.has_option('Main', 'sd_account')):
            log.warning(u"No sd_account or sd_url was found. Aborting.")
            sys.exit(2)
        endpoints = {}
        agentConfig['endpoints'] = endpoints
        if config.has_option('Main', 'sd_account'):
            agentConfig['sd_account'] = config.get('Main', 'sd_account')

        # Forwarder or not forwarder
        agentConfig['use_forwarder'] = options is not None and options.use_forwarder
        if agentConfig['use_forwarder']:
            listen_port = 17124
            if config.has_option('Main', 'listen_port'):
                listen_port = int(config.get('Main', 'listen_port'))
            agentConfig['sd_url'] = "http://" + agentConfig['bind_host'] + ":" + str(listen_port)
        elif config.has_option('Main', 'sd_url'):
            agentConfig['sd_url'] = config.get('Main', 'sd_url')
        else:
            # Default agent URL
            agentConfig['sd_url'] = "https://" + agentConfig['sd_account'] + ".local"

        if agentConfig['sd_url'].endswith('/'):
            agentConfig['sd_url'] = agentConfig['sd_url'][:-1]

        # Forwarder timeout
        agentConfig['forwarder_timeout'] = 20
        if config.has_option('Main', 'forwarder_timeout'):
            agentConfig['forwarder_timeout'] = int(config.get('Main', 'forwarder_timeout'))


        # Extra checks.d path
        # the linux directory is set by default
        if config.has_option('Main', 'additional_checksd'):
            agentConfig['additional_checksd'] = config.get('Main', 'additional_checksd')

        if config.has_option('Main', 'use_sdstatsd'):
            agentConfig['use_sdstatsd'] = config.get('Main', 'use_sdstatsd').lower() in ("yes", "true")
        else:
            agentConfig['use_sdstatsd'] = True

        # Service discovery
        if config.has_option('Main', 'service_discovery_backend'):
            try:
                additional_config = extract_agent_config(config)
                agentConfig.update(additional_config)
            except:
                log.error('Failed to load the agent configuration related to '
                          'service discovery. It will not be used.')

        # Concerns only Windows
        if config.has_option('Main', 'use_web_info_page'):
            agentConfig['use_web_info_page'] = config.get('Main', 'use_web_info_page').lower() in ("yes", "true")
        else:
            agentConfig['use_web_info_page'] = True

        # Which agent key to use
        agentConfig['agent_key'] = config.get('Main', 'agent_key')

        agentConfig['endpoints'][agentConfig['sd_url']] = [agentConfig['agent_key']]

        # local traffic only? Default to no
        agentConfig['non_local_traffic'] = False
        if config.has_option('Main', 'non_local_traffic'):
            agentConfig['non_local_traffic'] = config.get('Main', 'non_local_traffic').lower() in ("yes", "true")

        # DEPRECATED
        if config.has_option('Main', 'use_ec2_instance_id'):
            use_ec2_instance_id = config.get('Main', 'use_ec2_instance_id')
            # translate yes into True, the rest into False
            agentConfig['use_ec2_instance_id'] = (use_ec2_instance_id.lower() == 'yes')

        if config.has_option('Main', 'check_freq'):
            try:
                agentConfig['check_freq'] = int(config.get('Main', 'check_freq'))
            except Exception:
                pass

        # Custom histogram aggregate/percentile metrics
        if config.has_option('Main', 'histogram_aggregates'):
            agentConfig['histogram_aggregates'] = get_histogram_aggregates(config.get('Main', 'histogram_aggregates'))

        if config.has_option('Main', 'histogram_percentiles'):
            agentConfig['histogram_percentiles'] = get_histogram_percentiles(config.get('Main', 'histogram_percentiles'))

        # Disable Watchdog (optionally)
        if config.has_option('Main', 'watchdog'):
            if config.get('Main', 'watchdog').lower() in ('no', 'false'):
                agentConfig['watchdog'] = False

        # Optional graphite listener
        if config.has_option('Main', 'graphite_listen_port'):
            agentConfig['graphite_listen_port'] = \
                int(config.get('Main', 'graphite_listen_port'))
        else:
            agentConfig['graphite_listen_port'] = None

        # Sdstatsd config
        sdstatsd_defaults = {
            'sdstatsd_port': 8125,
            'sdstatsd_target': 'http://' + agentConfig['bind_host'] + ':17124',
        }
        for key, value in sdstatsd_defaults.iteritems():
            if config.has_option('Main', key):
                agentConfig[key] = config.get('Main', key)
            else:
                agentConfig[key] = value

        # Create app:xxx tags based on monitored apps
        agentConfig['create_sd_check_tags'] = config.has_option('Main', 'create_sd_check_tags') and \
            _is_affirmative(config.get('Main', 'create_sd_check_tags'))

        # Forwarding to external statsd server
        if config.has_option('Main', 'statsd_forward_host'):
            agentConfig['statsd_forward_host'] = config.get('Main', 'statsd_forward_host')
            if config.has_option('Main', 'statsd_forward_port'):
                agentConfig['statsd_forward_port'] = int(config.get('Main', 'statsd_forward_port'))

        # Optional config
        # FIXME not the prettiest code ever...
        if config.has_option('Main', 'use_mount'):
            agentConfig['use_mount'] = _is_affirmative(config.get('Main', 'use_mount'))

        if options is not None and options.autorestart:
            agentConfig['autorestart'] = True
        elif config.has_option('Main', 'autorestart'):
            agentConfig['autorestart'] = _is_affirmative(config.get('Main', 'autorestart'))

        if config.has_option('Main', 'check_timings'):
            agentConfig['check_timings'] = _is_affirmative(config.get('Main', 'check_timings'))

        if config.has_option('Main', 'exclude_process_args'):
            agentConfig['exclude_process_args'] = _is_affirmative(config.get('Main', 'exclude_process_args'))

        try:
            filter_device_re = config.get('Main', 'device_blacklist_re')
            agentConfig['device_blacklist_re'] = re.compile(filter_device_re)
        except ConfigParser.NoOptionError:
            pass

        # Dogstream config
        if config.has_option("Main", "dogstream_log"):
            # Older version, single log support
            log_path = config.get("Main", "dogstream_log")
            if config.has_option("Main", "dogstream_line_parser"):
                agentConfig["dogstreams"] = ':'.join([log_path, config.get("Main", "dogstream_line_parser")])
            else:
                agentConfig["dogstreams"] = log_path

        elif config.has_option("Main", "dogstreams"):
            agentConfig["dogstreams"] = config.get("Main", "dogstreams")

        if config.has_option("Main", "nagios_perf_cfg"):
            agentConfig["nagios_perf_cfg"] = config.get("Main", "nagios_perf_cfg")

        if config.has_option("Main", "use_curl_http_client"):
            agentConfig["use_curl_http_client"] = _is_affirmative(config.get("Main", "use_curl_http_client"))
        else:
            # Default to False as there are some issues with the curl client and ELB
            agentConfig["use_curl_http_client"] = False

        if config.has_option("Main", "allow_ipv6"):
            agentConfig["allow_ipv6"] = _is_affirmative(config.get("Main", "allow_ipv6"))
        else:
            agentConfig["allow_ipv6"] = True

        if config.has_section('WMI'):
            agentConfig['WMI'] = {}
            for key, value in config.items('WMI'):
                agentConfig['WMI'][key] = value

        if config.has_option("Main", "skip_ssl_validation"):
            agentConfig["skip_ssl_validation"] = _is_affirmative(config.get("Main", "skip_ssl_validation"))

        agentConfig["collect_instance_metadata"] = True
        if config.has_option("Main", "collect_instance_metadata"):
            agentConfig["collect_instance_metadata"] = _is_affirmative(config.get("Main", "collect_instance_metadata"))

        agentConfig["proxy_forbid_method_switch"] = False
        if config.has_option("Main", "proxy_forbid_method_switch"):
            agentConfig["proxy_forbid_method_switch"] = _is_affirmative(config.get("Main", "proxy_forbid_method_switch"))

        agentConfig["collect_ec2_tags"] = False
        if config.has_option("Main", "collect_ec2_tags"):
            agentConfig["collect_ec2_tags"] = _is_affirmative(config.get("Main", "collect_ec2_tags"))

        agentConfig["collect_orchestrator_tags"] = True
        if config.has_option("Main", "collect_orchestrator_tags"):
            agentConfig["collect_orchestrator_tags"] = _is_affirmative(config.get("Main", "collect_orchestrator_tags"))

        agentConfig["utf8_decoding"] = False
        if config.has_option("Main", "utf8_decoding"):
            agentConfig["utf8_decoding"] = _is_affirmative(config.get("Main", "utf8_decoding"))

        agentConfig["gce_updated_hostname"] = False
        if config.has_option("Main", "gce_updated_hostname"):
            agentConfig["gce_updated_hostname"] = _is_affirmative(config.get("Main", "gce_updated_hostname"))

        # APM config
        agentConfig["apm_enabled"] = True
        if config.has_option("Main", "apm_enabled"):
            agentConfig["apm_enabled"] = _is_affirmative(config.get("Main", "apm_enabled"))

        agentConfig["process_agent_enabled"] = False
        if config.has_option("Main", "process_agent_enabled"):
            agentConfig["process_agent_enabled"] = _is_affirmative(config.get("Main", "process_agent_enabled"))


        agentConfig["enable_gohai"] = True
        if config.has_option("Main", "enable_gohai"):
            agentConfig["enable_gohai"] = _is_affirmative(config.get("Main", "enable_gohai"))

        agentConfig["openstack_use_uuid"] = False
        if config.has_option("Main", "openstack_use_uuid"):
            agentConfig["openstack_use_uuid"] = _is_affirmative(config.get("Main", "openstack_use_uuid"))

        agentConfig["openstack_use_metadata_tags"] = True
        if config.has_option("Main", "openstack_use_metadata_tags"):
            agentConfig["openstack_use_metadata_tags"] = _is_affirmative(config.get("Main", "openstack_use_metadata_tags"))

    except ConfigParser.NoSectionError as e:
        sys.stderr.write('Config file not found or incorrectly formatted.\n')
        sys.exit(2)

    except ConfigParser.ParsingError as e:
        sys.stderr.write('Config file not found or incorrectly formatted.\n')
        sys.exit(2)

    except ConfigParser.NoOptionError as e:
        sys.stderr.write('There are some items missing from your config file, but nothing fatal [%s]' % e)

    # Storing proxy settings in the agentConfig
    agentConfig['proxy_settings'] = get_proxy(agentConfig)
    if agentConfig.get('ca_certs', None) is None:
        agentConfig['ssl_certificate'] = get_ssl_certificate(get_os(), 'sd-cert.pem')
    else:
        agentConfig['ssl_certificate'] = agentConfig['ca_certs']

    # On Windows, check for api key in registry if default api key
    # this code should never be used and is only a failsafe
    if Platform.is_windows() and agentConfig['api_key'] == 'APIKEYHERE' and can_query_registry:
        registry_conf = get_registry_conf(config)
        agentConfig.update(registry_conf)

    return agentConfig


def get_system_stats(proc_path=None):
    systemStats = {
        'machine': platform.machine(),
        'platform': sys.platform,
        'processor': platform.processor(),
        'pythonV': platform.python_version(),
    }

    platf = sys.platform

    try:
        if Platform.is_linux(platf):
            if not proc_path:
                proc_path = "/proc"
            proc_cpuinfo = os.path.join(proc_path,'cpuinfo')
            output, _, _ = get_subprocess_output(['grep', 'model name', proc_cpuinfo], log)
            systemStats['cpuCores'] = len(output.splitlines())

        if Platform.is_darwin(platf) or Platform.is_freebsd(platf):
            output, _, _ = get_subprocess_output(['sysctl', 'hw.ncpu'], log)
            systemStats['cpuCores'] = int(output.split(': ')[1])
    except SubprocessOutputEmptyError as e:
        log.warning("unable to retrieve number of cpuCores. Failed with error %s", e)

    if Platform.is_linux(platf):
        systemStats['nixV'] = platform.dist()

    elif Platform.is_darwin(platf):
        systemStats['macV'] = platform.mac_ver()

    elif Platform.is_freebsd(platf):
        version = platform.uname()[2]
        systemStats['fbsdV'] = ('freebsd', version, '')  # no codename for FreeBSD

    elif Platform.is_win32(platf):
        systemStats['winV'] = platform.win32_ver()

    return systemStats


def set_win32_cert_path():
    """In order to use tornado.httpclient with the packaged .exe on Windows we
    need to override the default ceritifcate location which is based on the path
    to tornado and will give something like "C:\path\to\program.exe\tornado/cert-file".

    If pull request #379 is accepted (https://github.com/facebook/tornado/pull/379) we
    will be able to override this in a clean way. For now, we have to monkey patch
    tornado.httpclient._DEFAULT_CA_CERTS
    """
    if hasattr(sys, 'frozen'):
        # we're frozen - from py2exe
        prog_path = os.path.dirname(sys.executable)
        crt_path = os.path.join(prog_path, 'ca-certificates.crt')
    else:
        cur_path = os.path.dirname(__file__)
        crt_path = os.path.join(cur_path, 'packaging', 'sd-agent', 'win32',
                                'install_files', 'ca-certificates.crt')
    import tornado.simple_httpclient
    log.info("Windows certificate path: %s" % crt_path)
    tornado.simple_httpclient._DEFAULT_CA_CERTS = crt_path


def set_win32_requests_ca_bundle_path():
    """In order to allow `requests` to validate SSL requests with the packaged .exe on Windows,
    we need to override the default certificate location which is based on the location of the
    requests or certifi libraries.

    We override the path directly in requests.adapters so that the override works even when the
    `requests` lib has already been imported
    """
    import requests.adapters
    if hasattr(sys, 'frozen'):
        # we're frozen - from py2exe
        prog_path = os.path.dirname(sys.executable)
        ca_bundle_path = os.path.join(prog_path, 'cacert.pem')
        requests.adapters.DEFAULT_CA_BUNDLE_PATH = ca_bundle_path

    log.info("Default CA bundle path of the requests library: {0}"
             .format(requests.adapters.DEFAULT_CA_BUNDLE_PATH))


def get_confd_path(osname=None):
    try:
        cur_path = os.path.dirname(os.path.realpath(__file__))
        return _confd_path(cur_path)
    except PathNotFound as e:
        pass

    bad_path = ''
    try:
        if Platform.is_windows():
            common_data = _windows_commondata_path()
            return _confd_path(os.path.join(common_data, 'Server Density'))
        elif Platform.is_mac():
            return _confd_path(MAC_CONFIG_PATH)
        else:
            return _confd_path(UNIX_CONFIG_PATH)
    except PathNotFound as e:
        if len(e.args) > 0:
            bad_path = e.args[0]

    raise PathNotFound(bad_path)


def get_checksd_path(osname=None):
    if Platform.is_windows():
        return _windows_checksd_path()
    # Mac & Linux
    else:
        # Unix only will look up based on the current directory
        # because checks.d will hang with the other python modules
        cur_path = os.path.dirname(os.path.realpath(__file__))
        return _checksd_path(cur_path)


def get_sdk_integrations_path(osname=None):
    if not osname:
        osname = get_os()

    if os.environ.get('INTEGRATIONS_DIR'):
        if os.environ.get('TRAVIS'):
            path = os.environ['TRAVIS_BUILD_DIR']
        elif os.environ.get('CIRCLECI'):
            path = os.path.join(
                os.environ['HOME'],
                os.environ['CIRCLE_PROJECT_REPONAME']
            )
        elif os.environ.get('APPVEYOR'):
            path = os.environ['APPVEYOR_BUILD_FOLDER']
        else:
            cur_path = os.environ['INTEGRATIONS_DIR']
            path = os.path.join(cur_path, '..') # might need tweaking in the future.
    else:
        cur_path = os.path.dirname(os.path.realpath(__file__))
        path = os.path.join(cur_path, '..', SDK_INTEGRATIONS_DIR)

    if os.path.exists(path):
        return path
    raise PathNotFound(path)

def get_jmx_pipe_path():
    if Platform.is_windows():
        pipe_path = SD_PIPE_WIN_PATH
    else:
        pipe_path = SD_PIPE_UNIX_PATH
        if not os.path.isdir(pipe_path):
            pipe_path = '/tmp'

    return pipe_path


def get_auto_confd_path(osname=None):
    """Used for service discovery which only works for Unix"""
    return os.path.join(get_confd_path(osname), AUTO_CONFIG_DIR)


def get_win32service_file(osname, filename):
    # This file is needed to log in the event viewer for windows
    if osname == 'windows':
        if hasattr(sys, 'frozen'):
            # we're frozen - from py2exe
            prog_path = os.path.dirname(sys.executable)
            path = os.path.join(prog_path, filename)
        else:
            cur_path = os.path.dirname(__file__)
            path = os.path.join(cur_path, filename)
        if os.path.exists(path):
            log.debug("Certificate file found at %s" % str(path))
            return path

    else:
        cur_path = os.path.dirname(os.path.realpath(__file__))
        path = os.path.join(cur_path, filename)
        if os.path.exists(path):
            return path

    return None


def get_ssl_certificate(osname, filename):
    # The SSL certificate is needed by tornado in case of connection through a proxy
    if osname == 'windows':
        if hasattr(sys, 'frozen'):
            # we're frozen - from py2exe
            prog_path = os.path.dirname(sys.executable)
            path = os.path.join(prog_path, filename)
        else:
            cur_path = os.path.dirname(__file__)
            path = os.path.join(cur_path, filename)
        if os.path.exists(path):
            log.debug("Certificate file found at %s" % str(path))
            return path
    else:
        cur_path = os.path.dirname(os.path.realpath(__file__))
        path = os.path.join(cur_path, filename)
        if os.path.exists(path):
            return path

    log.info("Certificate file NOT found at %s" % str(path))
    return None

def _get_check_module(check_name, check_path, from_site=False):
    error = None
    traceback_message = None
    if from_site:
        try:
            check_module = import_module("serverdensity_checks.{}".format(check_name))
        except Exception as e:
            error = e
            # Log at debug level since this code path is expected if the check is not installed as a wheel
            #log.debug('Unable to import check module %s from site-packages: %s', check_name, e)
    else:
        try:
            check_module = imp.load_source('checksd_%s' % check_name, check_path)
        except Exception as e:
            error = e
            traceback_message = traceback.format_exc()
            # There is a configuration file for that check but the module can't be imported
            log.exception('Unable to import check module %s.py from checks.d' % check_name)

    if error:
        return None, {'error': error, 'traceback': traceback_message}

    return check_module, None


def _get_wheel_version(check_name):
    check_module, err = _get_check_module(check_name, None, True)
    if err:
        return err

    if hasattr(check_module, "__version__"):
        return check_module.__version__

    return None

def _get_check_class(check_name, check_path, from_site=False):
    '''Return the corresponding check class for a check name if available.'''
    from checks import AgentCheck
    check_class = None

    check_module, err = _get_check_module(check_name, check_path, from_site)
    if err:
        return err

    # We make sure that there is an AgentCheck class defined
    check_class = None
    classes = inspect.getmembers(check_module, inspect.isclass)
    for _, clsmember in classes:
        if clsmember == AgentCheck:
            continue
        if issubclass(clsmember, AgentCheck):
            check_class = clsmember
            if AgentCheck in clsmember.__bases__:
                continue
            else:
                break
    return check_class


def _deprecated_configs(agentConfig):
    """ Warn about deprecated configs
    """
    deprecated_checks = {}
    deprecated_configs_enabled = [v for k, v in OLD_STYLE_PARAMETERS if len([l for l in agentConfig if l.startswith(k)]) > 0]
    for deprecated_config in deprecated_configs_enabled:
        msg = "Configuring %s in config.cfg is not supported anymore. Please use conf.d" % deprecated_config
        deprecated_checks[deprecated_config] = {'error': msg, 'traceback': None}
        log.error(msg)
    return deprecated_checks


def _file_configs_paths(osname, agentConfig):
    """ Retrieve all the file configs and return their paths
    """
    try:
        confd_path = get_confd_path(osname)
        all_file_configs = glob.glob(os.path.join(confd_path, '*.yaml'))
        all_default_configs = glob.glob(os.path.join(confd_path, '*.yaml.default'))
    except PathNotFound as e:
        log.error("No conf.d folder found at '%s' or in the directory where the Agent is currently deployed.\n" % e.args[0])
        sys.exit(3)

    if all_default_configs:
        current_configs = set([_conf_path_to_check_name(conf) for conf in all_file_configs])
        for default_config in all_default_configs:
            if not _conf_path_to_check_name(default_config) in current_configs:
                all_file_configs.append(default_config)

    # Compatibility code for the Nagios checks if it's still configured
    # in config.cfg
    # FIXME: 6.x, should be removed
    if not any('nagios' in config for config in itertools.chain(*all_file_configs)):
        # check if it's configured in config.cfg the old way
        if any([nagios_key in agentConfig for nagios_key in NAGIOS_OLD_CONF_KEYS]):
            all_file_configs.append('deprecated/nagios')

    return all_file_configs


def _service_disco_configs(agentConfig):
    """ Retrieve all the service disco configs and return their conf dicts
    """
    if agentConfig.get('service_discovery') and agentConfig.get('service_discovery_backend') in SD_BACKENDS:
        try:
            log.info("Fetching service discovery check configurations.")
            sd_backend = get_sd_backend(agentConfig=agentConfig)
            service_disco_configs = sd_backend.get_configs()
        except Exception:
            log.exception("Loading service discovery configurations failed.")
            return {}
    else:
        service_disco_configs = {}

    return service_disco_configs


def _conf_path_to_check_name(conf_path):
    f = os.path.splitext(os.path.split(conf_path)[1])
    if f[1] and f[1] == ".default":
        f = os.path.splitext(f[0])
    return f[0]


def get_checks_places(osname, agentConfig):
    """ Return a list of methods which, when called with a check name, will each return a check path to inspect
    """
    try:
        checksd_path = get_checksd_path(osname)
    except PathNotFound as e:
        log.info("no bundled checks.d path (checks provided as wheels): %s", e.args[0])
        checksd_path = None

    # custom checks
    places = [lambda name: (os.path.join(agentConfig['additional_checksd'], '%s.py' % name), None)]

    try:
        if Platform.is_windows():
            places.append(get_windows_sdk_check)
        else:
            sdk_integrations = get_sdk_integrations_path(osname)
            places.append(lambda name: (os.path.join(sdk_integrations, name, 'check.py'),
                                        os.path.join(sdk_integrations, name, 'manifest.json')))
    except PathNotFound:
        log.debug('No sdk integrations path found')

    # wheel integrations
    places.append(lambda name: (None, None))

    # agent-bundled integrations
    if checksd_path:
        places.append(lambda name: (os.path.join(checksd_path, '%s.py' % name), None))
    return places


def _load_file_config(config_path, check_name, agentConfig):
    if config_path == 'deprecated/nagios':
        log.warning("Configuring Nagios in config.cfg is deprecated "
                    "and will be removed in a future version. "
                    "Please use conf.d")
        check_config = {'instances': [dict((key, value) for (key, value) in agentConfig.iteritems() if key in NAGIOS_OLD_CONF_KEYS)]}
        return True, check_config, {}

    try:
        check_config = check_yaml(config_path)
    except Exception as e:
        log.exception("Unable to parse yaml config in %s" % config_path)
        traceback_message = traceback.format_exc()
        return False, None, {check_name: {'error': str(e), 'traceback': traceback_message, 'version': 'unknown'}}
    return True, check_config, {}


def get_valid_check_class(check_name, check_path, from_site=False):
    check_class = _get_check_class(check_name, check_path, from_site)

    if not check_class:
        log.error('No check class (inheriting from AgentCheck) found in %s.py' % check_name)
        return False, None, {}
    # this means we are actually looking at a load failure
    elif isinstance(check_class, dict):
        return False, None, {check_name: check_class}

    return True, check_class, {}


def _initialize_check(check_config, check_name, check_class, agentConfig,
                      manifest_path, version_override=None):
    init_config = check_config.get('init_config') or {}
    instances = check_config['instances']
    try:
        try:
            check = check_class(check_name, init_config=init_config,
                                agentConfig=agentConfig, instances=instances)
        except TypeError as e:
            # Backwards compatibility for checks which don't support the
            # instances argument in the constructor.
            check = check_class(check_name, init_config=init_config,
                                agentConfig=agentConfig)
            check.instances = instances

        if manifest_path:
            check.set_manifest_path(manifest_path)

        if not version_override:
            check.set_check_version(manifest=load_manifest(manifest_path))
        else:
            check.set_check_version(version=version_override)
    except Exception as e:
        log.exception('Unable to initialize check %s' % check_name)
        traceback_message = traceback.format_exc()
        manifest = load_manifest(manifest_path)
        if manifest is not None:
            check_version = '{core}:{vers}'.format(core=AGENT_VERSION,
                                                   vers=manifest.get('version', 'unknown'))
        elif version_override:
            check_version = version_override
        else:
            check_version = AGENT_VERSION

        return {}, {check_name: {'error': e, 'traceback': traceback_message, 'version': check_version}}
    else:
        return {check_name: check}, {}


def _update_python_path(check_config):
    # Add custom pythonpath(s) if available
    if 'pythonpath' in check_config:
        pythonpath = check_config['pythonpath']
        if not isinstance(pythonpath, list):
            pythonpath = [pythonpath]
        sys.path.extend(pythonpath)


def validate_sdk_check(manifest_path):
    max_validated = min_validated = False
    try:
        with open(manifest_path, 'r') as fp:
            manifest = json.load(fp)
            current_version = _version_string_to_tuple(get_version())
            for maxfield in MANIFEST_VALIDATION['max']:
                max_version = manifest.get(maxfield)
                if not max_version:
                    continue

                max_validated = _version_string_to_tuple(max_version) >= current_version
                break

            for minfield in MANIFEST_VALIDATION['min']:
                min_version = manifest.get(minfield)
                if not min_version:
                    continue

                min_validated = _version_string_to_tuple(min_version) <= current_version
                break
    except IOError:
        log.debug("Manifest file (%s) not present." % manifest_path)
    except json.JSONDecodeError:
        log.debug("Manifest file (%s) has badly formatted json." % manifest_path)
    except ValueError:
        log.debug("Versions in manifest file (%s) can't be validated.", manifest_path)

    return (min_validated and max_validated)


def load_check_from_places(check_config, check_name, checks_places, agentConfig):
    '''Find a check named check_name in the given checks_places and try to initialize it with the given check_config.
    A failure (`load_failure`) can happen when the check class can't be validated or when the check can't be initialized. '''
    load_success, load_failure = {}, {}
    for check_path_builder in checks_places:
        check_path, manifest_path = check_path_builder(check_name)

        is_wheel = not check_path and not manifest_path
        # The windows SDK function will return None,
        # so the loop should also continue if there is no path.
        if not (check_path and os.path.exists(check_path)) and not is_wheel:
            continue

        prev_failures = bool(load_failure)
        check_is_valid, check_class, load_failure = get_valid_check_class(check_name, check_path, from_site=is_wheel)
        if not check_is_valid:
            load_error = load_failure.get(check_name, {}).get('error')
            if is_wheel and not prev_failures and isinstance(load_error, ImportError):
                load_failure = {}
            continue

        if manifest_path:
            validated = validate_sdk_check(manifest_path)
            if not validated:
                log.warn("The SDK check (%s) was designed for a different agent core "
                         "or couldnt be validated - behavior is undefined" % check_name)

        version_override = None
        if is_wheel:
            wheel_version = _get_wheel_version(check_name)
            if wheel_version is None or isinstance(wheel_version, dict):
                version_override = 'Unknown Wheel'
            else:
                version_override = wheel_version
        elif not manifest_path and agentConfig['additional_checksd'] in check_path:
            version_override = 'custom'  # custom check


        load_success, load_failure = _initialize_check(
            check_config, check_name, check_class, agentConfig, manifest_path, version_override
        )

        _update_python_path(check_config)

        if is_wheel:
            log.debug('Loaded %s' % check_name)
        else:
            log.debug('Loaded %s' % check_path)
        break  # we successfully initialized this check

    return load_success, load_failure


def load_check_directory(agentConfig, hostname):
    ''' Return the initialized checks from checks.d, and a mapping of checks that failed to
    initialize. Only checks that have a configuration
    file in conf.d will be returned. '''
    from checks import AGENT_METRICS_CHECK_NAME
    from jmxfetch import JMX_CHECKS

    initialized_checks = {}
    init_failed_checks = {}
    deprecated_checks = {}
    agentConfig['checksd_hostname'] = hostname
    osname = get_os()

    # the TRACE_CONFIG flag is used by the configcheck to trace config object loading and
    # where they come from (service discovery, auto config or config file)
    if agentConfig.get(TRACE_CONFIG):
        configs_and_sources = {
            # check_name: (config_source, config)
        }

    deprecated_checks.update(_deprecated_configs(agentConfig))

    checks_places = get_checks_places(osname, agentConfig)

    for config_path in _file_configs_paths(osname, agentConfig):
        # '/usr/share/python/sd-agent/checks.d/my_check.py' -> 'my_check'
        check_name = _conf_path_to_check_name(config_path)

        conf_is_valid, check_config, invalid_check = _load_file_config(config_path, check_name, agentConfig)
        init_failed_checks.update(invalid_check)
        if not conf_is_valid:
            continue

        if agentConfig.get(TRACE_CONFIG):
            configs_and_sources[check_name] = (CONFIG_FROM_FILE, check_config)

        # load the check
        load_success, load_failure = load_check_from_places(check_config, check_name, checks_places, agentConfig)

        initialized_checks.update(load_success)
        init_failed_checks.update(load_failure)

    for check_name, service_disco_check_config in _service_disco_configs(agentConfig).iteritems():
        # ignore this config from service disco if the check has been loaded through a file config
        if check_name in initialized_checks or \
                check_name in init_failed_checks or \
                check_name in JMX_CHECKS:
            continue

        sd_init_config, sd_instances = service_disco_check_config[1]
        if agentConfig.get(TRACE_CONFIG):
            configs_and_sources[check_name] = (
                service_disco_check_config[0],
                {'init_config': sd_init_config, 'instances': sd_instances})

        check_config = {'init_config': sd_init_config, 'instances': sd_instances}

        # load the check
        load_success, load_failure = load_check_from_places(check_config, check_name, checks_places, agentConfig)

        initialized_checks.update(load_success)
        init_failed_checks.update(load_failure)

    init_failed_checks.update(deprecated_checks)
    log.info('initialized checks.d checks: %s' % [k for k in initialized_checks.keys() if k != AGENT_METRICS_CHECK_NAME])
    log.info('initialization failed checks.d checks: %s' % init_failed_checks.keys())

    if agentConfig.get(TRACE_CONFIG):
        return configs_and_sources

    return {'initialized_checks': initialized_checks.values(),
            'init_failed_checks': init_failed_checks}


def load_check(agentConfig, hostname, checkname):
    """Same logic as load_check_directory except it loads one specific check"""
    from jmxfetch import JMX_CHECKS

    agentConfig['checksd_hostname'] = hostname
    osname = get_os()
    checks_places = get_checks_places(osname, agentConfig)
    for config_path in _file_configs_paths(osname, agentConfig):
        check_name = _conf_path_to_check_name(config_path)
        if check_name == checkname and check_name not in JMX_CHECKS:
            conf_is_valid, check_config, invalid_check = _load_file_config(config_path, check_name, agentConfig)

            if invalid_check and not conf_is_valid:
                return invalid_check

            # try to load the check and return the result
            load_success, load_failure = load_check_from_places(check_config, check_name, checks_places, agentConfig)
            return load_success.values()[0] or load_failure

    # the check was not found, try with service discovery
    for check_name, service_disco_check_config in _service_disco_configs(agentConfig).iteritems():
        if check_name == checkname:
            sd_init_config, sd_instances = service_disco_check_config[1]
            check_config = {'init_config': sd_init_config, 'instances': sd_instances}

            # try to load the check and return the result
            load_success, load_failure = load_check_from_places(check_config, check_name, checks_places, agentConfig)
            return load_success.values()[0] if load_success else load_failure

    return None

def generate_jmx_configs(agentConfig, hostname, checknames=None):
    """Similar logic to load_check_directory for JMX checks"""
    from jmxfetch import get_jmx_checks

    jmx_checks = get_jmx_checks(auto_conf=True)

    if not checknames:
        checknames = jmx_checks
    agentConfig['checksd_hostname'] = hostname

    # the check was not found, try with service discovery
    generated = {}
    for check_name, service_disco_check_config in _service_disco_configs(agentConfig).iteritems():
        if check_name in checknames and check_name in jmx_checks:
            log.debug('Generating JMX config for: %s' % check_name)

            _, (sd_init_config, sd_instances) = service_disco_check_config

            check_config = {'init_config': sd_init_config,
                            'instances': sd_instances}

            try:
                yaml = config_to_yaml(check_config)
                generated["{}_{}".format(check_name, 0)] = yaml
            except Exception:
                log.exception("Unable to generate YAML config for %s", check_name)

    return generated

# logging

def get_log_date_format():
    return "%Y-%m-%d %H:%M:%S %Z"


def get_log_format(logger_name):
    if get_os() != 'windows':
        return '%%(asctime)s | %%(levelname)s | sd.%s | %%(name)s(%%(filename)s:%%(lineno)s) | %%(message)s' % logger_name
    return '%(asctime)s | %(levelname)s | %(name)s(%(filename)s:%(lineno)s) | %(message)s'


def get_syslog_format(logger_name):
    return 'sd.%s[%%(process)d]: %%(levelname)s (%%(filename)s:%%(lineno)s): %%(message)s' % logger_name


def get_logging_config(cfg_path=None):
    system_os = get_os()
    logging_config = {
        'log_level': None,
        'log_to_event_viewer': False,
        'log_to_syslog': False,
        'syslog_host': None,
        'syslog_port': None,
    }
    if system_os == 'windows':
        logging_config['collector_log_file'] = os.path.join(_windows_commondata_path(), 'ServerDensity', 'logs', 'collector.log')
        logging_config['forwarder_log_file'] = os.path.join(_windows_commondata_path(), 'ServerDensity', 'logs', 'forwarder.log')
        logging_config['sdstatsd_log_file'] = os.path.join(_windows_commondata_path(), 'ServerDensity', 'logs', 'sdstatsd.log')
        logging_config['jmxfetch_log_file'] = os.path.join(_windows_commondata_path(), 'ServerDensity', 'logs', 'jmxfetch.log')
        logging_config['service_log_file'] = os.path.join(_windows_commondata_path(), 'ServerDensity', 'logs', 'service.log')
        logging_config['log_to_syslog'] = False
    else:
        logging_config['collector_log_file'] = '/var/log/sd-agent/collector.log'
        logging_config['forwarder_log_file'] = '/var/log/sd-agent/forwarder.log'
        logging_config['sdstatsd_log_file'] = '/var/log/sd-agent/sdstatsd.log'
        logging_config['jmxfetch_log_file'] = '/var/log/sd-agent/jmxfetch.log'
        logging_config['go-metro_log_file'] = '/var/log/sd-agent/go-metro.log'
        logging_config['trace-agent_log_file'] = '/var/log/sd-agent/trace-agent.log'
        logging_config['process-agent_log_file'] = '/var/log/sd-agent/process-agent.log'
        logging_config['log_to_syslog'] = True

    config_path = get_config_path(cfg_path, os_name=system_os)
    config = ConfigParser.ConfigParser()
    config.readfp(skip_leading_wsp(open(config_path)))

    if config.has_section('handlers') or config.has_section('loggers') or config.has_section('formatters'):
        if system_os == 'windows':
            config_example_file = "https://github.com/serverdensity/sd-agent/blob/master/packaging/sd-agent/win32/install_files/config_win32.conf"
        else:
            config_example_file = "https://github.com/serverdensity/sd-agent/blob/master/config.cfg.example"

        sys.stderr.write("""Python logging config is no longer supported and will be ignored.
            To configure logging, update the logging portion of 'config.cfg' to match:
             '%s'.
             """ % config_example_file)

    for option in logging_config:
        if config.has_option('Main', option):
            logging_config[option] = config.get('Main', option)

    levels = {
        'CRITICAL': logging.CRITICAL,
        'DEBUG': logging.DEBUG,
        'ERROR': logging.ERROR,
        'FATAL': logging.FATAL,
        'INFO': logging.INFO,
        'WARN': logging.WARN,
        'WARNING': logging.WARNING,
    }
    if config.has_option('Main', 'log_level'):
        logging_config['log_level'] = levels.get(config.get('Main', 'log_level'))

    if config.has_option('Main', 'log_to_syslog'):
        logging_config['log_to_syslog'] = config.get('Main', 'log_to_syslog').strip().lower() in ['yes', 'true', 1]

    if config.has_option('Main', 'log_to_event_viewer'):
        logging_config['log_to_event_viewer'] = config.get('Main', 'log_to_event_viewer').strip().lower() in ['yes', 'true', 1]

    if config.has_option('Main', 'syslog_host'):
        host = config.get('Main', 'syslog_host').strip()
        if host:
            logging_config['syslog_host'] = host
        else:
            logging_config['syslog_host'] = None

    if config.has_option('Main', 'syslog_port'):
        port = config.get('Main', 'syslog_port').strip()
        try:
            logging_config['syslog_port'] = int(port)
        except Exception:
            logging_config['syslog_port'] = None

    if config.has_option('Main', 'disable_file_logging'):
        logging_config['disable_file_logging'] = config.get('Main', 'disable_file_logging').strip().lower() in ['yes', 'true', 1]
    else:
        logging_config['disable_file_logging'] = False

    return logging_config


def initialize_logging(logger_name):
    try:
        logging_config = get_logging_config()

        logging.basicConfig(
            format=get_log_format(logger_name),
            level=logging_config['log_level'] or logging.INFO,
        )

        log_file = logging_config.get('%s_log_file' % logger_name)
        if log_file is not None and not logging_config['disable_file_logging']:
            # make sure the log directory is writeable
            # NOTE: the entire directory needs to be writable so that rotation works
            if os.access(os.path.dirname(log_file), os.R_OK | os.W_OK):
                file_handler = logging.handlers.RotatingFileHandler(log_file, maxBytes=LOGGING_MAX_BYTES, backupCount=1)
                formatter = logging.Formatter(get_log_format(logger_name), get_log_date_format())
                file_handler.setFormatter(formatter)

                root_log = logging.getLogger()
                root_log.addHandler(file_handler)
            else:
                sys.stderr.write("Log file is unwritable: '%s'\n" % log_file)

        # set up syslog
        if logging_config['log_to_syslog']:
            try:
                from logging.handlers import SysLogHandler

                if logging_config['syslog_host'] is not None and logging_config['syslog_port'] is not None:
                    sys_log_addr = (logging_config['syslog_host'], logging_config['syslog_port'])
                else:
                    sys_log_addr = "/dev/log"
                    # Special-case BSDs
                    if Platform.is_darwin():
                        sys_log_addr = "/var/run/syslog"
                    elif Platform.is_freebsd():
                        sys_log_addr = "/var/run/log"

                handler = SysLogHandler(address=sys_log_addr, facility=SysLogHandler.LOG_DAEMON)
                handler.setFormatter(logging.Formatter(get_syslog_format(logger_name), get_log_date_format()))
                root_log = logging.getLogger()
                root_log.addHandler(handler)
            except Exception as e:
                sys.stderr.write("Error setting up syslog: '%s'\n" % str(e))
                traceback.print_exc()

        # Setting up logging in the event viewer for windows
        if get_os() == 'windows' and logging_config['log_to_event_viewer']:
            try:
                from logging.handlers import NTEventLogHandler
                nt_event_handler = NTEventLogHandler(logger_name, get_win32service_file('windows', 'win32service.pyd'), 'Application')
                nt_event_handler.setFormatter(logging.Formatter(get_syslog_format(logger_name), get_log_date_format()))
                nt_event_handler.setLevel(logging.ERROR)
                app_log = logging.getLogger(logger_name)
                app_log.addHandler(nt_event_handler)
            except Exception as e:
                sys.stderr.write("Error setting up Event viewer logging: '%s'\n" % str(e))
                traceback.print_exc()

    except Exception as e:
        sys.stderr.write("Couldn't initialize logging: %s\n" % str(e))
        traceback.print_exc()

        # if config fails entirely, enable basic stdout logging as a fallback
        logging.basicConfig(
            format=get_log_format(logger_name),
            level=logging.INFO,
        )

    # re-get the log after logging is initialized
    global log
    log = logging.getLogger(__name__)
