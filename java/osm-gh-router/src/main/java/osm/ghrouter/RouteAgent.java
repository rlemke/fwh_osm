package osm.ghrouter;

import afl.agent.AgentPoller;
import afl.agent.AgentPollerConfig;

import java.net.InetAddress;

/**
 * Entry point for the embedded-GraphHopper routing agent.
 *
 * Registers ONLY {@code osm.Routing.GraphHopper.RouteBatch} and polls the shared
 * MongoDB on the ``osm`` task list. Because claim_task is name-filtered, the
 * Python osm-geocoder runners (which don't have this handler) never take these
 * tasks, and this JVM never takes theirs — the two run side by side. Config is
 * read from the same AFL_* env the Python runners use.
 */
public final class RouteAgent {

    public static void main(String[] args) throws Exception {
        String mongo = env("FW_MONGODB_URL", "mongodb://afl-mongodb:27017");
        String database = env("FW_MONGODB_DATABASE", "facetwork");
        // RouteBatch tasks are tagged with their top-level namespace ("osm"),
        // so we must poll that list to claim them.
        String taskList = env("FW_TASK_LIST", "osm");
        String serverGroup = env("FW_SERVER_GROUP", "runner");
        String serverName = env("FW_FLEET_HOST", hostname());

        AgentPollerConfig config = new AgentPollerConfig(
                "osm-gh-router",   // serviceName
                serverGroup,       // serverGroup
                serverName,        // serverName
                taskList,          // taskList
                2000L,             // pollIntervalMs
                4,                 // maxConcurrent
                10000L,            // heartbeatIntervalMs
                mongo,             // mongoUrl
                database           // database
        );

        S3Util s3 = new S3Util();
        GraphHopperPool pool = new GraphHopperPool(s3);
        RouteBatchHandler handler = new RouteBatchHandler(pool, s3);

        AgentPoller poller = new AgentPoller(config);
        poller.register("osm.Routing.GraphHopper.RouteBatch", handler);

        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            poller.stop();
            pool.close();
            s3.close();
        }));

        System.out.println("[gh-router] starting: mongo=" + mongo + " db=" + database
                + " task_list=" + taskList + " server=" + serverName
                + " handlers=" + poller.registeredHandlers());
        poller.start();  // blocks
    }

    private static String env(String key, String dflt) {
        String v = System.getenv(key);
        return (v == null || v.isEmpty()) ? dflt : v;
    }

    private static String hostname() {
        try {
            return InetAddress.getLocalHost().getHostName();
        } catch (Exception e) {
            return "gh-router";
        }
    }
}
