#!/bin/sh
set -eu

envsubst '${SPINE_E2E_ACCESS_TOKEN} ${SPINE_E2E_IDENTITY_VERSION} ${SPINE_E2E_WORKSPACE_A_ID} ${SPINE_E2E_WORKSPACE_B_ID} ${SPINE_E2E_ENVIRONMENT_A_ID} ${SPINE_E2E_ENVIRONMENT_B_ID}' \
  < /opt/spine/runtime-config.js.template \
  > /usr/share/nginx/html/runtime-config.js
