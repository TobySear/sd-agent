# (C) Datadog, Inc. 2010-2016
# All rights reserved
# Licensed under Simplified BSD License (see LICENSE)

# stdlib
import collections
import locale
import logging
import pprint
import socket
import sys
import time

# 3p
try:
    import psutil
except ImportError:
    psutil = None

import simplejson as json

# project
from checks import AGENT_METRICS_CHECK_NAME, AgentCheck, create_service_check
from checks.check_status import (
    CheckStatus,
    CollectorStatus,
    EmitterStatus,
    STATUS_ERROR,
    STATUS_OK,
)
from checks.ganglia import Ganglia
from checks.server_density import plugins, yoshi
from config import get_system_stats, get_version
import checks.system.unix as u
import checks.system.win32 as w32
import modules
from util import get_uuid
from utils.cloud_metadata import GCE, EC2, CloudFoundry, Azure
from utils.logger import log_exceptions, RedactedLogRecord
from utils.jmx import JMXFiles
from utils.platform import Platform, get_os
from utils.subprocess_output import get_subprocess_output
from utils.timer import Timer
from utils.orchestrator import MetadataCollector

logging.LogRecord = RedactedLogRecord
log = logging.getLogger(__name__)


FLUSH_LOGGING_PERIOD = 10
FLUSH_LOGGING_INITIAL = 5
SD_CHECK_TAG = 'sd_check:{0}'


class AgentPayload(collections.MutableMapping):
    """
    AgentPayload offers a single payload interface but manages two payloads:
    * A metadata payload
    * A data payload that contains metrics, events, service_checks and more

    Each of these payloads is automatically submited to its specific endpoint.
    """
    METADATA_KEYS = frozenset(['meta', 'tags', 'host-tags', 'systemStats',
                               'agent_checks', 'gohai', 'external_host_tags'])

    DUPLICATE_KEYS = frozenset(['agentKey', 'agentVersion'])

    COMMON_ENDPOINT = ''
    DATA_ENDPOINT = 'metrics'
    METADATA_ENDPOINT = 'metadata'

    def __init__(self):
        self.data_payload = dict()
        self.meta_payload = dict()

    @property
    def payload(self):
        """
        Single payload with the content of data and metadata payloads.
        """
        res = self.data_payload.copy()
        res.update(self.meta_payload)

        return res

    def __getitem__(self, key):
        if key in self.METADATA_KEYS:
            return self.meta_payload[key]
        else:
            return self.data_payload[key]

    def __setitem__(self, key, value):
        if key in self.DUPLICATE_KEYS:
            self.data_payload[key] = value
            self.meta_payload[key] = value
        elif key in self.METADATA_KEYS:
            self.meta_payload[key] = value
        else:
            self.data_payload[key] = value

    def __delitem__(self, key):
        if key in self.DUPLICATE_KEYS:
            del self.data_payload[key]
            del self.meta_payload[key]
        elif key in self.METADATA_KEYS:
            del self.meta_payload[key]
        else:
            del self.data_payload[key]

    def __iter__(self):
        for item in self.data_payload:
            yield item
        for item in self.meta_payload:
            yield item

    def __len__(self):
        return len(self.data_payload) + len(self.meta_payload)

    def emit(self, log, config, emitters, continue_running, merge_payloads=True):
        """
        Send payloads via the emitters.

        :param merge_payloads: merge data and metadata payloads in a single payload and submit it
            to the common endpoint
        :type merge_payloads: boolean

        """
        statuses = []

        def _emit_payload(payload, endpoint):
            """ Send the payload via the emitters. """
            statuses = []
            for emitter in emitters:
                # Don't try to send to an emitter if we're stopping/
                if not continue_running:
                    return statuses
                name = emitter.__name__
                emitter_status = EmitterStatus(name)
                try:
                    emitter(payload, log, config, endpoint)
                except Exception as e:
                    log.exception("Error running emitter: %s"
                                  % emitter.__name__)
                    emitter_status = EmitterStatus(name, e)
                statuses.append(emitter_status)
            return statuses

        if merge_payloads:
            statuses.extend(_emit_payload(self.payload, self.COMMON_ENDPOINT))
        else:
            statuses.extend(_emit_payload(self.data_payload, self.DATA_ENDPOINT))
            statuses.extend(_emit_payload(self.meta_payload, self.METADATA_ENDPOINT))

        return statuses


class Collector(object):
    """
    The collector is responsible for collecting data from each check and
    passing it along to the emitters, who send it to their final destination.
    """
    def __init__(self, agentConfig, emitters, systemStats, hostname):
        self.emit_duration = None
        self.agentConfig = agentConfig
        self.hostname = hostname
        # system stats is generated by config.get_system_stats
        self.agentConfig['system_stats'] = systemStats
        # agent config is used during checks, system_stats can be accessed through the config
        self.os = get_os()
        self.plugins = None
        self.emitters = emitters
        self.check_timings = agentConfig.get('check_timings')
        self.push_times = {
            'host_metadata': {
                'start': time.time(),
                'interval': int(agentConfig.get('metadata_interval', 4 * 60 * 60))
            },
            'external_host_tags': {
                'start': time.time() - 3 * 60,  # Wait for the checks to init
                'interval': int(agentConfig.get('external_host_tags', 5 * 60))
            },
            'agent_checks': {
                'start': time.time(),
                'interval': int(agentConfig.get('agent_checks_interval', 10 * 60))
            },
            'processes': {
                'start': time.time(),
                'interval': int(agentConfig.get('processes_interval', 60))
            }
        }
        socket.setdefaulttimeout(15)
        self.run_count = 0
        self.continue_running = True
        self.hostname_metadata_cache = None
        self.initialized_checks_d = []
        self.init_failed_checks_d = {}

        if Platform.is_linux() and psutil is not None:
            procfs_path = agentConfig.get('procfs_path', '/proc').rstrip('/')
            psutil.PROCFS_PATH = procfs_path

        # Unix System Checks
        self._unix_system_checks = {
            'io': u.IO(log),
            'load': u.Load(log),
            'memory': u.Memory(log),
            'processes': u.Processes(log),
            'cpu': u.Cpu(log),
            'system': u.System(log),
            'file_handles': u.FileHandles(log)
        }

        # Server Density Checks
        self._server_density_checks = {
            'identifier': yoshi.Identifier(log),
            'plugins': plugins.Plugins(log)
        }

        # Win32 System `Checks
        self._win32_system_checks = {
            'io': w32.IO(log),
            'proc': w32.Processes(log),
            'memory': w32.Memory(log),
            'cpu': w32.Cpu(log),
            'system': w32.System(log)
        }

        # Old-style metric checks
        self._ganglia = Ganglia(log) if self.agentConfig.get('ganglia_host', '') != '' else None

        # Agent performance metrics check
        self._agent_metrics = None

        self._metrics_checks = []

        # Custom metric checks
        for module_spec in [s.strip() for s in self.agentConfig.get('custom_checks', '').split(',')]:
            if len(module_spec) == 0:
                continue
            try:
                self._metrics_checks.append(modules.load(module_spec, 'Check')(log))
                log.info("Registered custom check %s" % module_spec)
                log.warning("Old format custom checks are deprecated. They should be moved to the checks.d interface as old custom checks will be removed in a next version")
            except Exception:
                log.exception('Unable to load custom check module %s' % module_spec)

    def stop(self):
        """
        Tell the collector to stop at the next logical point.
        """
        # This is called when the process is being killed, so
        # try to stop the collector as soon as possible.
        # Most importantly, don't try to submit to the emitters
        # because the forwarder is quite possibly already killed
        # in which case we'll get a misleading error in the logs.
        # Best to not even try.
        self.continue_running = False
        for check in self.initialized_checks_d:
            check.stop()

    @staticmethod
    def _stats_for_display(raw_stats):
        return pprint.pformat(raw_stats, indent=4)

    @log_exceptions(log)
    def run(self, checksd=None, start_event=True, configs_reloaded=False):
        """
        Collect data from each check and submit their data.
        """
        log.debug("Found {num_checks} checks".format(num_checks=len(checksd['initialized_checks'])))
        timer = Timer()
        if not Platform.is_windows():
            cpu_clock = time.clock()
        self.run_count += 1
        log.debug("Starting collection run #%s" % self.run_count)

        if checksd:
            self.initialized_checks_d = checksd['initialized_checks']  # is a list of AgentCheck instances
            self.init_failed_checks_d = checksd['init_failed_checks']  # is of type {check_name: {error, traceback}}

        payload = AgentPayload()

        # Find the AgentMetrics check and pop it out
        # This check must run at the end of the loop to collect info on agent performance
        if not self._agent_metrics or configs_reloaded:
            for check in self.initialized_checks_d:
                if check.name == AGENT_METRICS_CHECK_NAME:
                    self._agent_metrics = check
                    self.initialized_checks_d.remove(check)
                    break

        # Initialize payload
        self._build_payload(payload)

        metrics = payload['metrics']
        events = payload['events']
        service_checks = payload['service_checks']

        # Run the system checks. Checks will depend on the OS
        if Platform.is_windows():
            # Win32 system checks
            for check_name in ['memory', 'cpu', 'io', 'proc', 'system']:
                try:
                    metrics.extend(self._win32_system_checks[check_name].check(self.agentConfig))
                except Exception:
                    log.exception('Unable to get %s metrics', check_name)
        else:

            sd_checks = self._server_density_checks

            identifier = sd_checks['identifier'].check(self.agentConfig)
            payload.update(identifier)

            # SDv1 plugins
            pluginsData = sd_checks['plugins'].check(self.agentConfig)
            if pluginsData:
                payload['plugins'] = pluginsData

            # Unix system checks
            sys_checks = self._unix_system_checks

            for check_name in ['load', 'system', 'cpu', 'file_handles']:
                try:
                    result_check = sys_checks[check_name].check(self.agentConfig)
                    if result_check:
                        payload.update(result_check)
                except Exception:
                    log.exception('Unable to get %s metrics', check_name)

            try:
                memory = sys_checks['memory'].check(self.agentConfig)
            except Exception:
                    log.exception('Unable to get memory metrics')
            else:
                if memory:
                    memstats = {
                        'memPhysUsed': memory.get('physUsed'),
                        'memPhysPctUsable': memory.get('physPctUsable'),
                        'memPhysFree': memory.get('physFree'),
                        'memPhysTotal': memory.get('physTotal'),
                        'memPhysUsable': memory.get('physUsable'),
                        'memSwapUsed': memory.get('swapUsed'),
                        'memSwapFree': memory.get('swapFree'),
                        'memSwapPctFree': memory.get('swapPctFree'),
                        'memSwapTotal': memory.get('swapTotal'),
                        'memCached': memory.get('physCached'),
                        'memBuffers': memory.get('physBuffers'),
                        'memShared': memory.get('physShared'),
                        'memSlab': memory.get('physSlab'),
                        'memPageTables': memory.get('physPageTables'),
                        'memSwapCached': memory.get('swapCached')
                    }
                    payload.update(memstats)

            try:
                ioStats = sys_checks['io'].check(self.agentConfig)
            except Exception:
                    log.exception('Unable to get io metrics')
            else:
                if ioStats:
                    payload['ioStats'] = ioStats

            try:
                processes = sys_checks['processes'].check(self.agentConfig)
            except Exception:
                    log.exception('Unable to get processes metrics')
            else:
                payload.update({'processes': processes})

        # Run old-style checks
        if self._ganglia is not None:
            payload['ganglia'] = self._ganglia.check(self.agentConfig)

        # process collector of gohai (compliant with payload of legacy "resources checks")
        if not Platform.is_windows() and self._should_send_additional_data('processes'):
            gohai_processes = self._run_gohai_processes()
            if gohai_processes:
                try:
                    gohai_processes_json = json.loads(gohai_processes)
                    processes_snaps = gohai_processes_json.get('processes')
                    if processes_snaps:
                        processes_payload = {
                            'snaps': [processes_snaps]
                        }

                        payload['resources'] = {
                            'processes': processes_payload,
                            'meta': {
                                'host': self.hostname,
                            }
                        }
                except Exception:
                    log.exception("Error running gohai processes collection")

        # newer-style checks (not checks.d style)
        for metrics_check in self._metrics_checks:
            res = metrics_check.check(self.agentConfig)
            if res:
                metrics.extend(res)

        # Use `info` log level for some messages on the first run only, then `debug`
        log_at_first_run = log.info if self._is_first_run() else log.debug

        # checks.d checks
        check_statuses = []
        for check in self.initialized_checks_d:
            if not self.continue_running:
                return
            log_at_first_run("Running check %s", check.name)
            instance_statuses = []
            metric_count = 0
            event_count = 0
            service_check_count = 0
            check_start_time = time.time()
            check_stats = None

            try:
                # Run the check.
                instance_statuses = check.run()

                # Collect the metrics and events.
                current_check_metrics = check.get_metrics()
                current_check_events = check.get_events()
                check_stats = check._get_internal_profiling_stats()

                # Collect metadata
                current_check_metadata = check.get_service_metadata()

                # Save metrics & events for the payload.
                metrics.extend(current_check_metrics)
                if current_check_events:
                    if check.name not in events:
                        events[check.name] = current_check_events
                    else:
                        events[check.name] += current_check_events

                # Save the status of the check.
                metric_count = len(current_check_metrics)
                event_count = len(current_check_events)

            except Exception:
                log.exception("Error running check %s" % check.name)

            check_status = CheckStatus(
                check.name, instance_statuses, metric_count,
                event_count, service_check_count, service_metadata=current_check_metadata,
                library_versions=check.get_library_info(),
                source_type_name=check.SOURCE_TYPE_NAME or check.name,
                check_stats=check_stats, check_version=check.check_version
            )

            # Service check for Agent checks failures
            service_check_tags = ["check:%s" % check.name]
            if check_status.status == STATUS_OK:
                status = AgentCheck.OK
            elif check_status.status == STATUS_ERROR:
                status = AgentCheck.CRITICAL
            check.service_check('serverdensity.agent.check_status', status, tags=service_check_tags)

            # Collect the service checks and save them in the payload
            current_check_service_checks = check.get_service_checks()
            if current_check_service_checks:
                service_checks.extend(current_check_service_checks)
            # -1 because the user doesn't care about the service check for check failure
            service_check_count = len(current_check_service_checks) - 1

            # Update the check status with the correct service_check_count
            check_status.service_check_count = service_check_count
            check_statuses.append(check_status)

            check_run_time = time.time() - check_start_time
            log.debug("Check %s ran in %.2f s" % (check.name, check_run_time))

            # Intrument check run timings if enabled.
            if self.check_timings:
                metric = 'serverdensity.agent.check_run_time'
                meta = {'tags': ["check:%s" % check.name]}
                metrics.append((metric, time.time(), check_run_time, meta))

        for check_name, info in self.init_failed_checks_d.iteritems():
            if not self.continue_running:
                return
            check_status = CheckStatus(check_name, None, None, None, None,
                                       check_version=info.get('version', 'unknown'),
                                       init_failed_error=info['error'],
                                       init_failed_traceback=info['traceback'])
            check_statuses.append(check_status)

        # Add a service check for the agent
        service_checks.append(create_service_check('serverdensity.agent.up', AgentCheck.OK,
                              hostname=self.hostname))

        # Store the metrics and events in the payload.
        payload['metrics'] = metrics
        payload['events'] = events
        payload['service_checks'] = service_checks

        # Populate metadata
        self._populate_payload_metadata(payload, check_statuses, start_event)

        collect_duration = timer.step()

        if self._agent_metrics:
            metric_context = {
                'collection_time': collect_duration,
                'emit_time': self.emit_duration,
            }
            if not Platform.is_windows():
                metric_context['cpu_time'] = time.clock() - cpu_clock

            self._agent_metrics.set_metric_context(payload, metric_context)
            self._agent_metrics.run()
            agent_stats = self._agent_metrics.get_metrics()
            payload['metrics'].extend(agent_stats)
            if self.agentConfig.get('developer_mode'):
                log.debug("\n Agent developer mode stats: \n {0}".format(
                    Collector._stats_for_display(agent_stats))
                )
            # Flush metadata for the Agent Metrics check. Otherwise they'll just accumulate and leak.
            self._agent_metrics.get_service_metadata()

        # Let's send our payload
        emitter_statuses = payload.emit(log, self.agentConfig, self.emitters,
                                        self.continue_running)
        self.emit_duration = timer.step()

        if self._is_first_run():
            # This is not the exact payload sent to the backend as minor post
            # processing is done, but this will give us a good idea of what is sent
            # to the backend.
            data = payload.payload # deep copy and merge of meta and metric data
            data['agent_key'] = '*************************' + data.get('agent_key', '')[-5:]
            # removing unused keys for the metadata payload
            if data.get('processes'):
                data['processes']['agent_key'] = '*************************' + data['processes'].get('agent_key', '')[-5:]
            log.debug("Metadata payload: %s", json.dumps(data))

        # Persist the status of the collection run.
        try:
            CollectorStatus(check_statuses, emitter_statuses,
                            self.hostname_metadata_cache).persist()
        except Exception:
            log.exception("Error persisting collector status")

        if self.run_count <= FLUSH_LOGGING_INITIAL or self.run_count % FLUSH_LOGGING_PERIOD == 0:
            log.info("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                     (self.run_count, round(collect_duration, 2), round(self.emit_duration, 2)))
            if self.run_count == FLUSH_LOGGING_INITIAL:
                log.info("First flushes done, next flushes will be logged every %s flushes." %
                         FLUSH_LOGGING_PERIOD)
        else:
            log.debug("Finished run #%s. Collection time: %ss. Emit time: %ss" %
                      (self.run_count, round(collect_duration, 2), round(self.emit_duration, 2)))

        return payload

    @staticmethod
    def run_single_check(check, verbose=True):
        log.info("Running check %s" % check.name)
        instance_statuses = []
        metric_count = 0
        event_count = 0
        service_check_count = 0
        check_stats = None

        try:
            # Run the check.
            instance_statuses = check.run()

            # Collect the metrics and events.
            current_check_metrics = check.get_metrics()
            current_check_events = check.get_events()
            current_service_checks = check.get_service_checks()
            current_service_metadata = check.get_service_metadata()

            check_stats = check._get_internal_profiling_stats()

            # Save the status of the check.
            metric_count = len(current_check_metrics)
            event_count = len(current_check_events)
            service_check_count = len(current_service_checks)

            print "Metrics: \n{0}".format(pprint.pformat(current_check_metrics))
            print "Events: \n{0}".format(pprint.pformat(current_check_events))
            print "Service Checks: \n{0}".format(pprint.pformat(current_service_checks))
            print "Service Metadata: \n{0}".format(pprint.pformat(current_service_metadata))

        except Exception:
            log.exception("Error running check %s" % check.name)

        check_status = CheckStatus(
            check.name, instance_statuses, metric_count,
            event_count, service_check_count,
            library_versions=check.get_library_info(),
            source_type_name=check.SOURCE_TYPE_NAME or check.name,
            check_stats=check_stats
        )

        return check_status

    def _emit(self, payload):
        """ Send the payload via the emitters. """
        statuses = []
        for emitter in self.emitters:
            # Don't try to send to an emitter if we're stopping/
            if not self.continue_running:
                return statuses
            name = emitter.__name__
            emitter_status = EmitterStatus(name)
            try:
                emitter(payload, log, self.agentConfig)
            except Exception as e:
                log.exception("Error running emitter: %s" % emitter.__name__)
                emitter_status = EmitterStatus(name, e)
            statuses.append(emitter_status)
        return statuses

    def _is_first_run(self):
        return self.run_count <= 1

    def _build_payload(self, payload):
        """
        Build the payload skeleton, so it contains all of the generic payload data.
        """
        now = time.time()

        payload['collection_timestamp'] = now
        payload['os'] = self.os
        payload['python'] = sys.version
        payload['agentVersion'] = self.agentConfig['version']
        payload['agentKey'] = self.agentConfig['agent_key']
        payload['events'] = {}
        payload['metrics'] = []
        payload['service_checks'] = []
        payload['resources'] = {}
        payload['internalHostname'] = self.hostname
        payload['uuid'] = get_uuid()
        payload['host-tags'] = {}
        payload['external_host_tags'] = {}

    def _populate_payload_metadata(self, payload, check_statuses, start_event=True):
        """
        Periodically populate the payload with metadata related to the system, host, and/or checks.
        """
        now = time.time()

        # Include system stats on first postback
        if start_event and self._is_first_run():
            payload['systemStats'] = self.agentConfig.get('system_stats', {})
            # Also post an event in the newsfeed
            payload['events']['System'] = [{
                'agent_key': self.agentConfig['agent_key'],
                'host': payload['internalHostname'],
                'timestamp': now,
                'event_type':'Agent Startup',
                'msg_text': 'Version %s' % get_version()
            }]

        # Periodically send the host metadata.
        if self._should_send_additional_data('host_metadata'):
            # gather metadata with gohai
            gohai_metadata = self._run_gohai_metadata()
            if gohai_metadata:
                payload['gohai'] = gohai_metadata

            payload['systemStats'] = get_system_stats(
                proc_path=self.agentConfig.get('procfs_path', '/proc').rstrip('/')
            )

            if self.agentConfig['collect_orchestrator_tags']:
                host_container_metadata = MetadataCollector().get_host_metadata()
                if host_container_metadata:
                    payload['container-meta'] = host_container_metadata

            payload['meta'] = self._get_hostname_metadata()

            self.hostname_metadata_cache = payload['meta']
            # Add static tags from the configuration file
            host_tags = []
            if self.agentConfig['tags'] is not None:
                host_tags.extend([unicode(tag.strip())
                                 for tag in self.agentConfig['tags'].split(",")])

            if self.agentConfig['collect_ec2_tags']:
                host_tags.extend(EC2.get_tags(self.agentConfig))

            if self.agentConfig['collect_orchestrator_tags']:
                host_docker_tags = MetadataCollector().get_host_tags()
                if host_docker_tags:
                    host_tags.extend(host_docker_tags)

            if host_tags:
                payload['host-tags']['system'] = host_tags

            # If required by the user, let's create the sd_check:xxx host tags
            if self.agentConfig['create_sd_check_tags']:
                app_tags_list = [SD_CHECK_TAG.format(c.name) for c in self.initialized_checks_d]
                app_tags_list.extend([SD_CHECK_TAG.format(cname) for cname
                                      in JMXFiles.get_jmx_appnames()])

                if 'system' not in payload['host-tags']:
                    payload['host-tags']['system'] = []

                payload['host-tags']['system'].extend(app_tags_list)

            GCE_tags = GCE.get_tags(self.agentConfig)
            if GCE_tags is not None:
                payload['host-tags'][GCE.SOURCE_TYPE_NAME] = GCE_tags

            # Log the metadata on the first run
            if self._is_first_run():
                log.info("Hostnames: %s, tags: %s" %
                         (repr(self.hostname_metadata_cache), payload['host-tags']))

        # Periodically send extra hosts metadata (vsphere)
        # Metadata of hosts that are not the host where the agent runs, not all the checks use
        # that
        external_host_tags = []
        if self._should_send_additional_data('external_host_tags'):
            for check in self.initialized_checks_d:
                try:
                    getter = getattr(check, 'get_external_host_tags')
                    check_tags = getter()
                    external_host_tags.extend(check_tags)
                except AttributeError:
                    pass

        if external_host_tags:
            payload['external_host_tags'] = external_host_tags

        # Periodically send agent_checks metadata
        if self._should_send_additional_data('agent_checks'):
            # Add agent checks statuses and error/warning messages
            agent_checks = []
            for check in check_statuses:
                if check.instance_statuses is not None:
                    for i, instance_status in enumerate(check.instance_statuses):
                        agent_checks.append(
                            (
                                check.name, check.source_type_name,
                                instance_status.instance_id,
                                instance_status.status,
                                # put error message or list of warning messages in the same field
                                # it will be handled by the UI
                                instance_status.error or instance_status.warnings or "",
                                check.service_metadata[i]
                            )
                        )
                else:
                    agent_checks.append(
                        (
                            check.name, check.source_type_name,
                            "initialization",
                            check.status, repr(check.init_failed_error)
                        )
                    )
            payload['agent_checks'] = agent_checks
            payload['meta'] = self.hostname_metadata_cache  # add hostname metadata

    def _get_hostname_metadata(self):
        """
        Returns a dictionnary that contains hostname metadata.
        """
        metadata = EC2.get_metadata(self.agentConfig)
        if metadata.get('hostname'):
            metadata['ec2-hostname'] = metadata.get('hostname')
            del metadata['hostname']

        if self.agentConfig.get('hostname'):
            metadata['agent-hostname'] = self.agentConfig.get('hostname')
        else:
            try:
                metadata["socket-hostname"] = socket.gethostname()
            except Exception:
                pass

        try:
            metadata["socket-fqdn"] = socket.getfqdn()
        except Exception:
            pass

        metadata["hostname"] = self.hostname
        metadata["timezones"] = self._decode_tzname(time.tzname)

        # Add cloud provider aliases
        if not metadata.get("host_aliases"):
            metadata["host_aliases"] = []

        host_aliases = GCE.get_host_aliases(self.agentConfig)
        if host_aliases:
            metadata['host_aliases'] += host_aliases

        # Try to get Azure VM ID
        host_aliases = Azure.get_host_aliases(self.agentConfig)
        if host_aliases:
            metadata['host_aliases'] += host_aliases

        try:
            metadata["host_aliases"] += CloudFoundry.get_host_aliases(self.agentConfig)
        except Exception:
            pass

        return metadata

    def _should_send_additional_data(self, data_name):
        if self._is_first_run():
            return True
        # If the interval has passed, send the metadata again
        now = time.time()
        if now - self.push_times[data_name]['start'] >= self.push_times[data_name]['interval']:
            log.debug('%s interval has passed. Sending it.' % data_name)
            self.push_times[data_name]['start'] = now
            return True

        return False

    def _run_gohai_metadata(self):
        return self._run_gohai(['--exclude', 'processes'])

    def _run_gohai_processes(self):
        return self._run_gohai(['--only', 'processes'])

    def _run_gohai(self, options):
        # Gohai is disabled on Mac for now
        if Platform.is_mac() or not self.agentConfig.get('enable_gohai'):
            return None
        output = None
        try:
            output, err, _ = get_subprocess_output(["gohai"] + options, log)
            if err:
                log.debug("GOHAI LOG | %s", err)
        except OSError as e:
            if e.errno == 2:  # file not found, expected when install from source
                log.debug("gohai file not found")
            else:
                log.warning("Unexpected OSError when running gohai %s", e)
        except Exception as e:
            log.warning("gohai command failed with error %s", e)

        return output

    @staticmethod
    def _decode_tzname(tzname):
        """ On Windows, decodes the timezone from the system-preferred encoding
        """
        if Platform.is_windows():
            try:
                decoded_tzname = map(lambda tz: tz.decode(locale.getpreferredencoding()), tzname)
            except Exception:
                log.exception("Failed decoding timezone with encoding %s", locale.getpreferredencoding())
                return ('', '')
            return tuple(decoded_tzname)

        return tzname
