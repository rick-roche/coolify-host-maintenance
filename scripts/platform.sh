#!/usr/bin/env bash

hostcare_supported_os() {
  local os_id=$1 version_id=$2
  [[ $os_id == ubuntu && ( $version_id == 22.04 || $version_id == 24.04 || $version_id == 26.04 ) ]]
}
