# Embedded-GraphHopper routing agent — a JVM runner that joins the fleet via the
# fw-agent AgentPoller and serves osm.Routing.GraphHopper.RouteBatch alongside the
# Python osm runners. Java 17 matches the graphhopper-core 8.0 the graphs were
# built with.
FROM eclipse-temurin:17-jre

WORKDIR /app

# The shaded fat jar (fw-agent + graphhopper-core 8.0 + AWS SDK + handler).
# Build it first:  mvn -q -DskipTests package
COPY target/osm-gh-router.jar /app/osm-gh-router.jar

# GraphHopper loads a whole state graph into heap; give it room.
ENV JAVA_TOOL_OPTIONS="-Xmx4g"

ENTRYPOINT ["java", "-jar", "/app/osm-gh-router.jar"]
