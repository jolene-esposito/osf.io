#!/bin/bash

export HOME=$(mktemp -d)
cd /opt/apps/osf
source /opt/data/envs/osf/bin/activate

mkdir -p $HOME/.config/matplotlib
invoke analytics >> /var/log/osf/analytics.log 2>&1
