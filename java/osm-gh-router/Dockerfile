# Embedded-GraphHopper routing agent — a JVM runner that joins the fleet via the
# fw-agent AgentPoller and serves osm.Routing.GraphHopper.RouteBatch alongside the
# Python osm runners. Java 17 matches the graphhopper-core 8.0 the graphs were
# built with.
#
# Self-contained + reproducible: builds the fw-agent SDK and the shaded fat jar
# from source inside the image — no local mvn, ~/.m2, or pre-built target/ jar
# needed. Build context is THIS directory (java/osm-gh-router):
#
#   docker build -t osm-gh-router:latest .
#
# Run it against the shared infra (mirrors the env the Python osm runners use):
#   docker run -d --name gh-router --network <osm-net> \
#     -e AFL_MONGODB_URL=mongodb://mongodb:27017 -e AFL_MONGODB_DATABASE=facetwork \
#     -e AFL_S3_ENDPOINT=http://<minio>:9000 -e AFL_S3_REGION=us-east-1 \
#     -e AFL_S3_ACCESS_KEY=... -e AFL_S3_SECRET_KEY=... \
#     -e AFL_OSM_OUTPUT_BASE=s3://afl-cache/osm-output -e AFL_TASK_LIST=osm \
#     osm-gh-router:latest

# ── Stage 1: build the fat jar (fw-agent SDK + graphhopper-core + AWS SDK) ────
FROM maven:3.9-eclipse-temurin-17 AS build

# The fw-agent SDK lives in the facetwork repo; build + install it to the local
# Maven repo so this module can depend on afl:fw-agent. Pin a ref with
# --build-arg FW_AGENT_REF=<branch|tag|sha> (default main).
ARG FW_AGENT_REF=main
RUN git clone --depth 1 --branch "${FW_AGENT_REF}" \
        https://github.com/rlemke/facetwork.git /tmp/facetwork \
    && mvn -q -f /tmp/facetwork/agents/java/fw-agent/pom.xml -DskipTests install \
    && rm -rf /tmp/facetwork/.git

WORKDIR /build
# Copy the POM first so dependency resolution caches when only sources change.
COPY pom.xml .
RUN mvn -q -DskipTests dependency:go-offline || true
COPY src ./src
RUN mvn -q -DskipTests clean package

# ── Stage 2: runtime ─────────────────────────────────────────────────────────
FROM eclipse-temurin:17-jre
WORKDIR /app
COPY --from=build /build/target/osm-gh-router.jar /app/osm-gh-router.jar
# GraphHopper loads/imports a whole state graph into heap; give it room.
ENV JAVA_TOOL_OPTIONS="-Xmx4g"
ENTRYPOINT ["java", "-jar", "/app/osm-gh-router.jar"]
