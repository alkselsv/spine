#!/bin/sh
set -eu

envsubst '${SPINE_WEB_ACCESS_TOKEN} ${SPINE_WEB_IDENTITY_VERSION} ${SPINE_WEB_WORKSPACE_A_ID} ${SPINE_WEB_WORKSPACE_B_ID} ${SPINE_WEB_ENVIRONMENT_A_ID} ${SPINE_WEB_ENVIRONMENT_B_ID}' \
  < /opt/spine/runtime-config.js.template \
  > /usr/share/nginx/html/runtime-config.js
