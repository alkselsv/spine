FROM neo4j:5.26.32-community@sha256:f1e52d4dc6da122468c4a3c1050a4f5e117f6d3268625237c34d49166f0d6082

# Cognee 1.5.4 uses APOC to assign dynamic labels. The official Neo4j image
# ships the matching APOC Core jar in /labs but does not enable it by default.
RUN cp /var/lib/neo4j/labs/apoc-*-core.jar /var/lib/neo4j/plugins/

# Cognee generates URL-safe random passwords, which may begin with "-". The
# upstream entrypoint otherwise lets neo4j-admin parse such a password as an
# option and the dataset container never reaches Bolt readiness.
RUN sed -i 's/set-initial-password "${password}"/set-initial-password -- "${password}"/' \
    /startup/docker-entrypoint.sh

ENV NEO4J_dbms_security_procedures_allowlist="apoc.*"
ENV NEO4J_dbms_security_procedures_unrestricted="apoc.*"
