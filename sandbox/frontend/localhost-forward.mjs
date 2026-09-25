// Forwards 127.0.0.1:<port> inside the frontend container to the same port on
// the Docker host, for the port in NEXT_PUBLIC_SUPABASE_URL when that URL
// points at localhost. Sandbox-only: see the note in the Dockerfile.
import net from "node:net";

const raw = process.env.NEXT_PUBLIC_SUPABASE_URL || "";
const target = process.env.SANDBOX_HOST_GATEWAY || "host.docker.internal";

let url;
try {
  url = new URL(raw);
} catch {
  console.log(`[localhost-forward] NEXT_PUBLIC_SUPABASE_URL not a URL (${raw}); nothing to forward`);
  process.exit(0);
}

if (!["localhost", "127.0.0.1"].includes(url.hostname)) {
  console.log(`[localhost-forward] ${url.hostname} is not localhost; nothing to forward`);
  process.exit(0);
}

const port = Number(url.port || (url.protocol === "https:" ? 443 : 80));
net
  .createServer((client) => {
    const upstream = net.connect(port, target);
    client.pipe(upstream).pipe(client);
    const close = () => {
      client.destroy();
      upstream.destroy();
    };
    client.on("error", close);
    upstream.on("error", (err) => {
      console.error(`[localhost-forward] ${target}:${port} unreachable: ${err.message}`);
      close();
    });
  })
  .listen(port, "127.0.0.1", () =>
    console.log(`[localhost-forward] 127.0.0.1:${port} -> ${target}:${port}`),
  );
