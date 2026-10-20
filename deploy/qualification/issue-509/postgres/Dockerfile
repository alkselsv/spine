ARG POSTGRES_IMAGE=pgvector/pgvector:0.8.7-pg17
FROM ${POSTGRES_IMAGE}

COPY init-qualification.sh /docker-entrypoint-initdb.d/10-qualification.sh
RUN chmod 0755 /docker-entrypoint-initdb.d/10-qualification.sh
