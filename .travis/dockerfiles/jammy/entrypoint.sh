#!/bin/bash
sudo sed -i "s|trusty|$RELEASE|" /sd-agent*/debian/changelog
ubuntu=(bionic focal jammy xenial trusty)
if [[ ${ubuntu[*]} =~ "$RELEASE" ]]
then
    distro="ubuntu"
else
    distro="debian"
fi
sudo cp -a /sd-agent/debian/distros/"$RELEASE"/. /sd-agent/debian
sudo dpkg-source -b /sd-agent
for arch in amd64 arm64; do
    if [ ! -d /packages/"$distro"/"$RELEASE" ]; then
        sudo mkdir -p /packages/"$distro"/"$RELEASE"
    fi
    if [ ! -d /packages/"$distro"/"$RELEASE"/"$arch" ]; then
        sudo mkdir -p /packages/"$distro"/"$RELEASE"/"$arch"
    fi
    pbuilder-dist $RELEASE $arch update
    pbuilder-dist $RELEASE $arch build \
    --buildresult /packages/"$distro"/"$RELEASE"/"$arch" *"$RELEASE"*.dsc
done;
