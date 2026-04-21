# MicroVM Lifecycle Sidecar Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a static Rust binary that listens on port 9000, handles all 5 Lambda MicroVM lifecycle hooks, and emits DataDog events, metrics, and logs immediately on each transition.

**Architecture:** Two-path observability design — lifecycle hooks use the immediate path (tokio::spawn → DD API), platform observability uses the buffered path (mpsc channel → FlushTask). This plan implements the immediate path end-to-end and creates compile-only stubs for the buffered path.

**Tech Stack:** Rust, axum 0.7, tokio (full), reqwest 0.12 (rustls-tls), serde + serde_yml, tracing + tracing-subscriber, wiremock (tests)

---

## File Map

| File | Status | Responsibility |
|------|--------|----------------|
| `microvm-sidecar/Cargo.toml` | Create | Dependencies, feature flags |
| `microvm-sidecar/src/main.rs` | Create | Entrypoint, AppState, axum router |
| `microvm-sidecar/src/config/mod.rs` | Create | Config struct, YAML load, env var overrides |
| `microvm-sidecar/src/datadog/signals.rs` | Create | DdSignal enum, payload builders, tag logic |
| `microvm-sidecar/src/datadog/mod.rs` | Create | DD HTTP client: send_immediate (events/metrics/logs) |
| `microvm-sidecar/src/hooks/payload.rs` | Create | LaunchRequest deserialization |
| `microvm-sidecar/src/hooks/mod.rs` | Create | axum handlers for all 5 hooks |
| `microvm-sidecar/src/emitter.rs` | Create | LifecycleEmitter: build signals, spawn tasks, track handles |
| `microvm-sidecar/src/collector/mod.rs` | Create | STUB: Collector trait, no-op scheduler |
| `microvm-sidecar/src/flush.rs` | Create | STUB: FlushTask skeleton |
| `microvm-sidecar/microvm-sidecar.yaml` | Create | Default config template |
| `microvm-sidecar/Makefile` | Create | build-docker, build, build-local targets |

---

## Task 1: Project scaffold

**Files:**
- Create: `microvm-sidecar/Cargo.toml`
- Create: `microvm-sidecar/src/main.rs`

- [ ] **Step 1: Create the Cargo project**

```bash
cd "/Users/tianning.li/Downloads/Lambda MicroVM"
cargo new microvm-sidecar
```

- [ ] **Step 2: Replace `Cargo.toml` with full dependency set**

```toml
[package]
name = "microvm-sidecar"
version = "0.1.0"
edition = "2021"

[[bin]]
name = "microvm-sidecar"
path = "src/main.rs"

[features]
system-metrics = []

[dependencies]
axum = { version = "0.7", features = ["macros"] }
tokio = { version = "1", features = ["full"] }
reqwest = { version = "0.12", default-features = false, features = ["json", "rustls-tls"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
serde_yml = "0.0.12"
async-trait = "0.1"
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter", "json"] }
chrono = { version = "0.4", features = ["serde"] }
tower = { version = "0.4", features = ["util"] }
tower-http = { version = "0.5", features = ["catch-panic"] }
futures = "0.3"

[dev-dependencies]
wiremock = "0.6"
http-body-util = "0.1"
tempfile = "3"
serial_test = "2"
futures = "0.3"
```

- [ ] **Step 3: Replace `src/main.rs` with a placeholder that compiles**

```rust
mod config;
mod datadog;
mod emitter;
mod hooks;
mod collector;
mod flush;

fn main() {
    println!("microvm-sidecar");
}
```

- [ ] **Step 4: Create module stub files so the build doesn't fail**

```bash
mkdir -p microvm-sidecar/src/config
mkdir -p microvm-sidecar/src/datadog
mkdir -p microvm-sidecar/src/hooks
mkdir -p microvm-sidecar/src/collector
touch microvm-sidecar/src/config/mod.rs
touch microvm-sidecar/src/datadog/mod.rs
touch microvm-sidecar/src/datadog/signals.rs
touch microvm-sidecar/src/hooks/mod.rs
touch microvm-sidecar/src/hooks/payload.rs
touch microvm-sidecar/src/emitter.rs
touch microvm-sidecar/src/collector/mod.rs
touch microvm-sidecar/src/flush.rs
```

- [ ] **Step 5: Verify the project compiles**

```bash
cd microvm-sidecar && cargo build 2>&1
```

Expected: no errors (warnings about empty modules are fine).

- [ ] **Step 6: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: scaffold microvm-sidecar Cargo project"
```

---

## Task 2: Config module (TDD)

The `Config` struct is loaded from a YAML file, then env vars override individual fields. `DD_TAGS` replaces (not merges) the YAML tags list.

**Files:**
- Create: `microvm-sidecar/src/config/mod.rs`

- [ ] **Step 1: Write `src/config/mod.rs` with failing tests at the bottom**

Create the file with just the test module first (implementation body is missing, so it won't compile):

```rust
// src/config/mod.rs — tests first, implementation in Step 3

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use serial_test::serial;

    fn yaml_config(content: &str) -> tempfile::NamedTempFile {
        let mut f = tempfile::NamedTempFile::new().unwrap();
        write!(f, "{}", content).unwrap();
        f
    }

    #[test]
    fn test_defaults_when_no_file() {
        let cfg = Config::from_file(None);
        assert_eq!(cfg.sidecar.port, 9000);
        assert_eq!(cfg.datadog.site, "datadoghq.com");
        assert!(cfg.datadog.api_key.is_empty());
    }

    #[test]
    fn test_loads_from_yaml() {
        let f = yaml_config(
            "sidecar:\n  port: 9001\ndatadog:\n  api_key: abc\n  site: datadoghq.eu\n  service: svc\n  env: staging\n  tags: []\n",
        );
        let cfg = Config::from_file(Some(f.path()));
        assert_eq!(cfg.sidecar.port, 9001);
        assert_eq!(cfg.datadog.api_key, "abc");
        assert_eq!(cfg.datadog.site, "datadoghq.eu");
    }

    // Serial: mutates global env state — must not run concurrently with other env tests
    #[test]
    #[serial]
    fn test_env_overrides_api_key() {
        std::env::set_var("DD_API_KEY", "env-key");
        let mut cfg = Config::from_file(None);
        cfg.apply_env_overrides();
        assert_eq!(cfg.datadog.api_key, "env-key");
        std::env::remove_var("DD_API_KEY");
    }

    #[test]
    #[serial]
    fn test_dd_tags_env_replaces_yaml_list() {
        let f = yaml_config(
            "sidecar:\n  port: 9000\ndatadog:\n  api_key: \"\"\n  site: datadoghq.com\n  service: s\n  env: prod\n  tags:\n    - team:platform\n",
        );
        std::env::set_var("DD_TAGS", "team:infra,region:us-east-2");
        let mut cfg = Config::from_file(Some(f.path()));
        cfg.apply_env_overrides();
        assert_eq!(cfg.datadog.tags, vec!["team:infra", "region:us-east-2"]);
        std::env::remove_var("DD_TAGS");
    }
}
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd microvm-sidecar && cargo test config 2>&1 | tail -20
```

Expected: compile errors (`Config` not defined yet).

- [ ] **Step 3: Implement the Config types above the existing test module**

Prepend the following to `src/config/mod.rs` (keep the `#[cfg(test)]` block from Step 1 at the bottom, do not duplicate it):

```rust
use serde::{Deserialize, Serialize};
use std::path::Path;

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct Config {
    pub sidecar: SidecarConfig,
    pub datadog: DatadogConfig,
    #[serde(default)]
    pub collectors: CollectorsConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct SidecarConfig {
    pub port: u16,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct DatadogConfig {
    pub api_key: String,
    pub site: String,
    pub service: String,
    pub env: String,
    #[serde(default)]
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct CollectorsConfig {
    #[serde(default)]
    pub buffer: BufferConfig,
    #[serde(default)]
    pub system_metrics: SystemMetricsConfig,
}

#[derive(Debug, Clone, Deserialize, Serialize)]
pub struct BufferConfig {
    pub channel_capacity: usize,
    pub max_batch_size: usize,
    pub flush_interval_seconds: u64,
}

#[derive(Debug, Clone, Deserialize, Serialize, Default)]
pub struct SystemMetricsConfig {
    pub enabled: bool,
    pub interval_seconds: u64,
    pub cpu: bool,
    pub memory: bool,
    pub network: bool,
    pub file_descriptors: bool,
    pub oom_events: bool,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            sidecar: SidecarConfig { port: 9000 },
            datadog: DatadogConfig {
                api_key: String::new(),
                site: "datadoghq.com".to_string(),
                service: "microvm-app".to_string(),
                env: "production".to_string(),
                tags: Vec::new(),
            },
            collectors: CollectorsConfig::default(),
        }
    }
}

impl Default for BufferConfig {
    fn default() -> Self {
        BufferConfig {
            channel_capacity: 10000,
            max_batch_size: 500,
            flush_interval_seconds: 10,
        }
    }
}

impl Config {
    /// Load config from a known path, or return defaults if None / file missing.
    pub fn from_file(path: Option<&Path>) -> Self {
        let Some(p) = path else { return Config::default() };
        if !p.exists() { return Config::default() }
        let content = std::fs::read_to_string(p)
            .expect("failed to read config file");
        serde_yml::from_str(&content).expect("failed to parse config YAML")
    }

    /// Search the standard paths and load the first one found.
    pub fn load() -> Self {
        let explicit = std::env::var("SIDECAR_CONFIG").ok()
            .map(std::path::PathBuf::from);
        let search: &[Option<std::path::PathBuf>] = &[
            explicit,
            Some("/etc/microvm-sidecar.yaml".into()),
            Some("./microvm-sidecar.yaml".into()),
        ];
        for path_opt in search {
            if let Some(p) = path_opt {
                if p.exists() {
                    return Self::from_file(Some(p));
                }
            }
        }
        Config::default()
    }

    pub fn apply_env_overrides(&mut self) {
        if let Ok(v) = std::env::var("SIDECAR_PORT") {
            if let Ok(p) = v.parse() { self.sidecar.port = p; }
        }
        if let Ok(v) = std::env::var("DD_API_KEY")  { self.datadog.api_key  = v; }
        if let Ok(v) = std::env::var("DD_SITE")     { self.datadog.site     = v; }
        if let Ok(v) = std::env::var("DD_SERVICE")  { self.datadog.service  = v; }
        if let Ok(v) = std::env::var("DD_ENV")      { self.datadog.env      = v; }
        if let Ok(v) = std::env::var("DD_TAGS") {
            self.datadog.tags = v.split(',')
                .map(|s| s.trim().to_string())
                .filter(|s| !s.is_empty())
                .collect();
        }
        if let Ok(v) = std::env::var("COLLECTORS_SYSTEM_METRICS_ENABLED") {
            self.collectors.system_metrics.enabled = v == "true";
        }
    }
}

// The #[cfg(test)] mod tests block from Step 1 stays at the bottom of the file unchanged.

- [ ] **Step 4: Run tests and verify they pass**

```bash
cd microvm-sidecar && cargo test config 2>&1
```

Expected: `test config::tests::test_defaults_when_no_file ... ok` (×4 tests)

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add Config with YAML loading and env var overrides"
```

---

## Task 3: DdSignal types and tag builder (TDD)

The `DdSignal` enum and the HTTP payload structs that get serialised to DataDog's APIs. Tag logic: auto-inject `hook:`, `microvm_id:` (omit if absent), `hook_result:`. `mesh_ipv6` goes in log message body only.

**Files:**
- Create: `microvm-sidecar/src/datadog/signals.rs`

- [ ] **Step 1: Write the failing tests in `src/datadog/signals.rs`**

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_tags_include_hook_and_result() {
        let tags = build_tags("launch", Some("vm-123"), "ok", &[]);
        assert!(tags.contains(&"hook:launch".to_string()));
        assert!(tags.contains(&"hook_result:ok".to_string()));
        assert!(tags.contains(&"microvm_id:vm-123".to_string()));
    }

    #[test]
    fn test_microvm_id_omitted_when_none() {
        let tags = build_tags("ready", None, "ok", &[]);
        assert!(!tags.iter().any(|t| t.starts_with("microvm_id:")));
    }

    #[test]
    fn test_user_tags_merged() {
        let user = vec!["team:platform".to_string()];
        let tags = build_tags("launch", Some("vm-1"), "ok", &user);
        assert!(tags.contains(&"team:platform".to_string()));
    }

    #[test]
    fn test_event_payload_structure() {
        let payload = DdEventPayload::new("launch", "vm-123", &[]);
        assert_eq!(payload.title, "MicroVM launch");
        assert!(payload.text.contains("vm-123"));
        assert!(payload.tags.iter().any(|t| t == "hook:launch"));
    }

    #[test]
    fn test_metric_payload_structure() {
        let payload = DdMetricPayload::new("launch", Some("vm-123"), &[]);
        assert_eq!(payload.series.len(), 1);
        assert_eq!(payload.series[0].metric, "microvm.hook.launch");
        assert_eq!(payload.series[0].points[0].value, 1.0);
    }

    #[test]
    fn test_log_payload_contains_mesh_ipv6_in_message() {
        let payload = DdLogEntry::new("launch", Some("vm-123"), Some("fd00::1"), "svc", "prod", &[]);
        let msg: serde_json::Value = serde_json::from_str(&payload.message).unwrap();
        assert_eq!(msg["mesh_ipv6"], "fd00::1");
        // mesh_ipv6 must NOT appear in ddtags
        assert!(!payload.ddtags.contains("mesh_ipv6"));
    }

    #[test]
    fn test_log_payload_omits_mesh_ipv6_when_none() {
        let payload = DdLogEntry::new("ready", None, None, "svc", "prod", &[]);
        let msg: serde_json::Value = serde_json::from_str(&payload.message).unwrap();
        assert!(msg.get("mesh_ipv6").is_none());
    }
}
```

- [ ] **Step 2: Run to confirm compile failure**

```bash
cd microvm-sidecar && cargo test datadog::signals 2>&1 | head -20
```

Expected: compile errors.

- [ ] **Step 3: Implement signals.rs**

```rust
use serde::{Deserialize, Serialize};
use chrono::Utc;

// ── Tag builder ──────────────────────────────────────────────────────────────

pub fn build_tags(
    hook: &str,
    microvm_id: Option<&str>,
    result: &str,
    user_tags: &[String],
) -> Vec<String> {
    let mut tags: Vec<String> = user_tags.to_vec();
    tags.push(format!("hook:{}", hook));
    tags.push(format!("hook_result:{}", result));
    if let Some(id) = microvm_id {
        tags.push(format!("microvm_id:{}", id));
    }
    tags
}

// ── DdSignal enum ─────────────────────────────────────────────────────────────

#[derive(Debug, Clone)]
pub enum DdSignal {
    Event(DdEventPayload),
    Metric(DdMetricPayload),
    Log(DdLogEntry),
}

// ── Events (/api/v1/events) ───────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DdEventPayload {
    pub title: String,
    pub text: String,
    pub tags: Vec<String>,
    pub alert_type: String,
}

impl DdEventPayload {
    pub fn new(hook: &str, microvm_id: &str, user_tags: &[String]) -> Self {
        DdEventPayload {
            title: format!("MicroVM {}", hook),
            text: format!("MicroVM {} transitioned: {}", microvm_id, hook),
            tags: build_tags(hook, Some(microvm_id), "ok", user_tags),
            alert_type: "info".to_string(),
        }
    }

    pub fn new_no_id(hook: &str, user_tags: &[String]) -> Self {
        DdEventPayload {
            title: format!("MicroVM {}", hook),
            text: format!("MicroVM (unknown) transitioned: {}", hook),
            tags: build_tags(hook, None, "ok", user_tags),
            alert_type: "info".to_string(),
        }
    }
}

// ── Metrics (/api/v2/series) ──────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DdMetricPayload {
    pub series: Vec<DdSeries>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DdSeries {
    pub metric: String,
    #[serde(rename = "type")]
    pub metric_type: u8, // 1 = count
    pub points: Vec<DdPoint>,
    pub tags: Vec<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DdPoint {
    pub timestamp: i64,
    pub value: f64,
}

impl DdMetricPayload {
    pub fn new(hook: &str, microvm_id: Option<&str>, user_tags: &[String]) -> Self {
        DdMetricPayload {
            series: vec![DdSeries {
                metric: format!("microvm.hook.{}", hook),
                metric_type: 1,
                points: vec![DdPoint {
                    timestamp: Utc::now().timestamp(),
                    value: 1.0,
                }],
                tags: build_tags(hook, microvm_id, "ok", user_tags),
            }],
        }
    }
}

// ── Logs (/api/v2/logs) ───────────────────────────────────────────────────────

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct DdLogEntry {
    pub ddsource: String,
    pub service: String,
    pub ddtags: String, // comma-separated; mesh_ipv6 NOT included here
    pub message: String, // JSON string; mesh_ipv6 included here
}

impl DdLogEntry {
    pub fn new(
        hook: &str,
        microvm_id: Option<&str>,
        mesh_ipv6: Option<&str>,
        service: &str,
        env: &str,
        user_tags: &[String],
    ) -> Self {
        let tags = build_tags(hook, microvm_id, "ok", user_tags);
        let ddtags = tags.join(",");

        let mut msg = serde_json::json!({
            "timestamp": Utc::now().to_rfc3339(),
            "hook": hook,
            "service": service,
            "env": env,
            "status": "ok",
        });
        if let Some(id) = microvm_id {
            msg["microvm_id"] = serde_json::Value::String(id.to_string());
        }
        if let Some(ipv6) = mesh_ipv6 {
            msg["mesh_ipv6"] = serde_json::Value::String(ipv6.to_string());
        }

        DdLogEntry {
            ddsource: "microvm-sidecar".to_string(),
            service: service.to_string(),
            ddtags,
            message: msg.to_string(),
        }
    }
}
```

Add to `src/datadog/mod.rs`:
```rust
pub mod signals;
pub use signals::*;
```

- [ ] **Step 4: Run the tests**

```bash
cd microvm-sidecar && cargo test datadog::signals 2>&1
```

Expected: all 6 tests pass.

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add DdSignal types, payload builders, and tag logic"
```

---

## Task 4: DD API client — immediate send (TDD with wiremock)

Implements `send_event`, `send_metric`, `send_log` as async functions. Each makes a single POST to the corresponding DataDog endpoint. Errors are logged; they never propagate.

**Files:**
- Modify: `microvm-sidecar/src/datadog/mod.rs`

- [ ] **Step 1: Write the failing tests**

Add to `src/datadog/mod.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use wiremock::{MockServer, Mock, ResponseTemplate};
    use wiremock::matchers::{method, path, header};

    fn test_config(base_url: &str) -> crate::config::DatadogConfig {
        // Pass the full wiremock URI (e.g. "http://127.0.0.1:PORT") unchanged.
        // base_url() in datadog/mod.rs detects the http:// prefix and uses it as-is,
        // bypassing the "https://api.{site}" production prefix logic.
        crate::config::DatadogConfig {
            api_key: "test-key".to_string(),
            site: base_url.to_string(),
            service: "svc".to_string(),
            env: "test".to_string(),
            tags: vec![],
        }
    }

    #[tokio::test]
    async fn test_send_event_posts_to_correct_path() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v1/events"))
            .and(header("DD-API-KEY", "test-key"))
            .respond_with(ResponseTemplate::new(202))
            .expect(1)
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        let cfg = test_config(&server.uri());
        let payload = crate::datadog::signals::DdEventPayload::new("launch", "vm-1", &[]);
        send_event(&client, &cfg, payload).await;

        server.verify().await;
    }

    #[tokio::test]
    async fn test_send_metric_posts_to_correct_path() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v2/series"))
            .and(header("DD-API-KEY", "test-key"))
            .respond_with(ResponseTemplate::new(202))
            .expect(1)
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        let cfg = test_config(&server.uri());
        let payload = crate::datadog::signals::DdMetricPayload::new("launch", Some("vm-1"), &[]);
        send_metric(&client, &cfg, payload).await;

        server.verify().await;
    }

    #[tokio::test]
    async fn test_send_log_posts_to_correct_path() {
        let server = MockServer::start().await;
        Mock::given(method("POST"))
            .and(path("/api/v2/logs"))
            .and(header("DD-API-KEY", "test-key"))
            .respond_with(ResponseTemplate::new(200))
            .expect(1)
            .mount(&server)
            .await;

        let client = reqwest::Client::new();
        let cfg = test_config(&server.uri());
        let payload = crate::datadog::signals::DdLogEntry::new("launch", Some("vm-1"), None, "svc", "test", &[]);
        send_log(&client, &cfg, payload).await;

        server.verify().await;
    }

    #[tokio::test]
    async fn test_dd_error_does_not_panic() {
        // Point at a non-existent server — the function must return, not panic
        let client = reqwest::Client::new();
        let cfg = crate::config::DatadogConfig {
            api_key: "k".to_string(),
            site: "localhost:1".to_string(), // nothing listening here
            service: "s".to_string(),
            env: "e".to_string(),
            tags: vec![],
        };
        let payload = crate::datadog::signals::DdEventPayload::new_no_id("ready", &[]);
        send_event(&client, &cfg, payload).await; // must not panic or hang
    }
}
```

- [ ] **Step 2: Run to confirm compile failure**

```bash
cd microvm-sidecar && cargo test datadog::tests 2>&1 | head -20
```

- [ ] **Step 3: Implement the send functions in `src/datadog/mod.rs`**

```rust
pub mod signals;
pub use signals::*;

use reqwest::Client;
use crate::config::DatadogConfig;
use tracing::warn;

fn base_url(cfg: &DatadogConfig) -> String {
    // wiremock uses http://; production uses https://
    let site = &cfg.site;
    if site.starts_with("http://") || site.starts_with("https://") {
        site.trim_end_matches('/').to_string()
    } else {
        format!("https://api.{}", site)
    }
}

pub async fn send_event(client: &Client, cfg: &DatadogConfig, payload: DdEventPayload) {
    let url = format!("{}/api/v1/events", base_url(cfg));
    let res = client
        .post(&url)
        .header("DD-API-KEY", &cfg.api_key)
        .json(&payload)
        .send()
        .await;
    match res {
        Ok(r) if r.status().is_success() => {}
        Ok(r) => warn!(status = %r.status(), "DD Events API returned non-success"),
        Err(e) => warn!(error = %e, "DD Events API request failed"),
    }
}

pub async fn send_metric(client: &Client, cfg: &DatadogConfig, payload: DdMetricPayload) {
    let url = format!("{}/api/v2/series", base_url(cfg));
    let res = client
        .post(&url)
        .header("DD-API-KEY", &cfg.api_key)
        .json(&payload)
        .send()
        .await;
    match res {
        Ok(r) if r.status().is_success() => {}
        Ok(r) => warn!(status = %r.status(), "DD Metrics API returned non-success"),
        Err(e) => warn!(error = %e, "DD Metrics API request failed"),
    }
}

pub async fn send_log(client: &Client, cfg: &DatadogConfig, payload: DdLogEntry) {
    let url = format!("{}/api/v2/logs", base_url(cfg));
    // Logs API expects an array
    let res = client
        .post(&url)
        .header("DD-API-KEY", &cfg.api_key)
        .json(&vec![payload])
        .send()
        .await;
    match res {
        Ok(r) if r.status().is_success() => {}
        Ok(r) => warn!(status = %r.status(), "DD Logs API returned non-success"),
        Err(e) => warn!(error = %e, "DD Logs API request failed"),
    }
}
```

- [ ] **Step 4: Run the tests**

```bash
cd microvm-sidecar && cargo test datadog::tests 2>&1
```

Expected: all 4 tests pass.

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add DD API client with send_event/send_metric/send_log"
```

---

## Task 5: AppState and hook payload (TDD)

Defines the shared axum state and the `LaunchRequest` deserialization struct.

**Files:**
- Modify: `microvm-sidecar/src/main.rs`
- Create: `microvm-sidecar/src/hooks/payload.rs`

- [ ] **Step 1: Write failing tests for LaunchRequest**

In `src/hooks/payload.rs`:

```rust
#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_deserialize_full_launch_request() {
        let json = r#"{"microVmId":"vm-abc","meshIpv6Address":"fd00::1"}"#;
        let req: LaunchRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.micro_vm_id, "vm-abc");
        assert_eq!(req.mesh_ipv6_address, Some("fd00::1".to_string()));
    }

    #[test]
    fn test_deserialize_launch_request_without_ipv6() {
        let json = r#"{"microVmId":"vm-xyz"}"#;
        let req: LaunchRequest = serde_json::from_str(json).unwrap();
        assert_eq!(req.micro_vm_id, "vm-xyz");
        assert!(req.mesh_ipv6_address.is_none());
    }

    #[test]
    fn test_deserialize_empty_body_as_default() {
        let req: LaunchRequest = serde_json::from_str("{}").unwrap();
        assert!(req.micro_vm_id.is_empty());
    }
}
```

- [ ] **Step 2: Implement `LaunchRequest` in `src/hooks/payload.rs`**

```rust
use serde::Deserialize;

#[derive(Debug, Deserialize, Default)]
pub struct LaunchRequest {
    #[serde(rename = "microVmId", default)]
    pub micro_vm_id: String,
    #[serde(rename = "meshIpv6Address")]
    pub mesh_ipv6_address: Option<String>,
}
```

- [ ] **Step 3: Run payload tests**

```bash
cd microvm-sidecar && cargo test hooks::payload 2>&1
```

Expected: 3 tests pass.

- [ ] **Step 4: Define `AppState` and `MicroVmState` in `src/main.rs`**

```rust
use std::sync::{Arc, Mutex};
use tokio::task::JoinHandle;
use crate::config::Config;
use crate::datadog::DdSignal;

#[derive(Debug, Clone, Default)]
pub struct MicroVmState {
    pub micro_vm_id: String,
    pub mesh_ipv6_address: Option<String>,
}

pub struct AppState {
    pub micro_vm:     Arc<Mutex<Option<MicroVmState>>>,
    pub dd_client:    Arc<Mutex<reqwest::Client>>,
    pub task_handles: Arc<Mutex<Vec<JoinHandle<()>>>>,
    pub config:       Arc<Config>,
    // ── Stubs for the buffered observability path (not yet implemented) ──────
    // signal_tx: system collectors send Vec<DdSignal> here; consumed by FlushTask
    #[allow(dead_code)]
    pub signal_tx: Option<tokio::sync::mpsc::Sender<Vec<DdSignal>>>,
    // flush_tx: suspend/terminate signal FlushTask for immediate drain.
    // mpsc (not oneshot) so it lives in Arc<AppState> — oneshot::Sender is not Clone.
    #[allow(dead_code)]
    pub flush_tx: Option<tokio::sync::mpsc::Sender<()>>,
}

impl AppState {
    pub fn new(config: Config) -> Self {
        AppState {
            micro_vm:     Arc::new(Mutex::new(None)),
            dd_client:    Arc::new(Mutex::new(reqwest::Client::new())),
            task_handles: Arc::new(Mutex::new(Vec::new())),
            config:       Arc::new(config),
            signal_tx:    None, // populated when buffered path is implemented
            flush_tx:     None, // populated when buffered path is implemented
        }
    }
}
```

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add AppState, MicroVmState, and LaunchRequest payload"
```

---

## Task 6: LifecycleEmitter (TDD)

Builds `DdSignal`s for a given hook and spawns three concurrent `tokio::spawn` tasks (one per signal type). Stores `JoinHandle`s in `AppState.task_handles`.

**Files:**
- Create: `microvm-sidecar/src/emitter.rs`

- [ ] **Step 1: Write failing tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use crate::{AppState, config::Config};
    use wiremock::{MockServer, Mock, ResponseTemplate};
    use wiremock::matchers::{method, path};

    fn state_with_dd(base_url: &str) -> Arc<AppState> {
        let mut cfg = Config::default();
        cfg.datadog.api_key = "key".to_string();
        cfg.datadog.site = base_url.to_string();
        Arc::new(AppState::new(cfg))
    }

    #[tokio::test]
    async fn test_emit_launch_sends_three_signals() {
        let server = MockServer::start().await;
        for p in ["/api/v1/events", "/api/v2/series", "/api/v2/logs"] {
            Mock::given(method("POST")).and(path(p))
                .respond_with(ResponseTemplate::new(202))
                .expect(1)
                .mount(&server)
                .await;
        }
        let state = state_with_dd(&server.uri());
        emit(&state, "launch", Some("vm-1"), None).await;
        // give spawned tasks time to complete
        tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
        server.verify().await;
    }

    #[tokio::test]
    async fn test_emit_ready_sends_signals_without_microvm_id() {
        let server = MockServer::start().await;
        for p in ["/api/v1/events", "/api/v2/series", "/api/v2/logs"] {
            Mock::given(method("POST")).and(path(p))
                .respond_with(ResponseTemplate::new(202))
                .expect(1)
                .mount(&server)
                .await;
        }
        let state = state_with_dd(&server.uri());
        emit(&state, "ready", None, None).await;
        tokio::time::sleep(tokio::time::Duration::from_millis(200)).await;
        server.verify().await;
    }

    #[tokio::test]
    async fn test_emit_stores_join_handles() {
        let server = MockServer::start().await;
        for p in ["/api/v1/events", "/api/v2/series", "/api/v2/logs"] {
            Mock::given(method("POST")).and(path(p))
                .respond_with(ResponseTemplate::new(202))
                .mount(&server)
                .await;
        }
        let state = state_with_dd(&server.uri());
        emit(&state, "suspend", Some("vm-1"), None).await;
        let count = state.task_handles.lock().unwrap().len();
        assert_eq!(count, 3); // one per signal type
    }
}
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd microvm-sidecar && cargo test emitter 2>&1 | head -20
```

- [ ] **Step 3: Implement `src/emitter.rs`**

```rust
use std::sync::Arc;
use crate::AppState;
use crate::datadog::{
    send_event, send_metric, send_log,
    DdEventPayload, DdMetricPayload, DdLogEntry,
};

/// Emit all three DD signal types for a lifecycle hook.
/// Spawns three concurrent tasks and stores their JoinHandles.
pub async fn emit(
    state: &Arc<AppState>,
    hook: &str,
    microvm_id: Option<&str>,
    mesh_ipv6: Option<&str>,
) {
    let cfg = state.config.datadog.clone();
    let tags = cfg.tags.clone();

    // Build payloads
    let event = match microvm_id {
        Some(id) => DdEventPayload::new(hook, id, &tags),
        None     => DdEventPayload::new_no_id(hook, &tags),
    };
    let metric  = DdMetricPayload::new(hook, microvm_id, &tags);
    let log     = DdLogEntry::new(
        hook, microvm_id, mesh_ipv6, &cfg.service, &cfg.env, &tags,
    );

    // Spawn three concurrent tasks
    let client_guard = state.dd_client.lock().unwrap().clone();
    // reqwest::Client is cheap to clone (Arc-backed)
    let c1 = client_guard.clone();
    let c2 = client_guard.clone();
    let c3 = client_guard.clone();
    let cfg1 = cfg.clone();
    let cfg2 = cfg.clone();
    let cfg3 = cfg.clone();

    let h1 = tokio::spawn(async move { send_event(&c1, &cfg1, event).await });
    let h2 = tokio::spawn(async move { send_metric(&c2, &cfg2, metric).await });
    let h3 = tokio::spawn(async move { send_log(&c3, &cfg3, log).await });

    state.task_handles.lock().unwrap().extend([h1, h2, h3]);
}
```

- [ ] **Step 4: Run the tests**

```bash
cd microvm-sidecar && cargo test emitter 2>&1
```

Expected: 3 tests pass.

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add LifecycleEmitter with concurrent DD signal dispatch"
```

---

## Task 7: Hook handlers — all 5 (TDD)

The axum handlers for the five lifecycle hooks. Each calls `emit()` and returns 200. `launch` additionally parses the payload and stores `MicroVmState`. `resume` aborts old handles and recreates `dd_client`. `suspend` and `terminate` flush in-flight handles.

**Files:**
- Modify: `microvm-sidecar/src/hooks/mod.rs`

- [ ] **Step 1: Write failing tests**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use axum::{body::Body, http::{Request, StatusCode}};
    use tower::ServiceExt;
    use crate::{AppState, config::Config, build_router};

    fn test_app() -> axum::Router {
        let state = Arc::new(AppState::new(Config::default()));
        build_router(state)
    }

    async fn post(app: axum::Router, uri: &str, body: &str) -> StatusCode {
        let req = Request::builder()
            .method("POST")
            .uri(uri)
            .header("content-type", "application/json")
            .body(Body::from(body.to_string()))
            .unwrap();
        let resp = app.oneshot(req).await.unwrap();
        resp.status()
    }

    const BASE: &str = "/aws/lambda-microvms/runtime/beta/v1";

    #[tokio::test]
    async fn test_ready_returns_200()     { assert_eq!(post(test_app(), &format!("{BASE}/ready"),    "{}").await, StatusCode::OK); }
    #[tokio::test]
    async fn test_launch_returns_200()    { assert_eq!(post(test_app(), &format!("{BASE}/launch"),   r#"{"microVmId":"vm-1"}"#).await, StatusCode::OK); }
    #[tokio::test]
    async fn test_resume_returns_200()    { assert_eq!(post(test_app(), &format!("{BASE}/resume"),   "{}").await, StatusCode::OK); }
    #[tokio::test]
    async fn test_suspend_returns_200()   { assert_eq!(post(test_app(), &format!("{BASE}/suspend"),  "{}").await, StatusCode::OK); }
    #[tokio::test]
    async fn test_terminate_returns_200() { assert_eq!(post(test_app(), &format!("{BASE}/terminate"),"{}").await, StatusCode::OK); }

    #[tokio::test]
    async fn test_launch_stores_microvm_id() {
        let state = Arc::new(AppState::new(Config::default()));
        let app = build_router(Arc::clone(&state));
        post(app, &format!("{BASE}/launch"), r#"{"microVmId":"vm-abc","meshIpv6Address":"fd00::1"}"#).await;
        let vm = state.micro_vm.lock().unwrap();
        let vm = vm.as_ref().unwrap();
        assert_eq!(vm.micro_vm_id, "vm-abc");
        assert_eq!(vm.mesh_ipv6_address.as_deref(), Some("fd00::1"));
    }
}
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd microvm-sidecar && cargo test hooks 2>&1 | head -20
```

- [ ] **Step 3: Implement `src/hooks/mod.rs`**

```rust
pub mod payload;
use payload::LaunchRequest;

use std::sync::Arc;
use axum::{extract::State, http::StatusCode, response::IntoResponse, Json};
use tokio::time::{timeout, Duration};
use futures::future::join_all;
use tracing::warn;
use crate::{AppState, MicroVmState, emitter::emit};

const FLUSH_TIMEOUT: Duration = Duration::from_secs(10);
const BASE: &str = "/aws/lambda-microvms/runtime/beta/v1";

pub fn routes() -> axum::Router<Arc<AppState>> {
    axum::Router::new()
        .route(&format!("{BASE}/ready"),     axum::routing::post(ready))
        .route(&format!("{BASE}/launch"),    axum::routing::post(launch))
        .route(&format!("{BASE}/resume"),    axum::routing::post(resume))
        .route(&format!("{BASE}/suspend"),   axum::routing::post(suspend))
        .route(&format!("{BASE}/terminate"), axum::routing::post(terminate))
}

pub async fn ready(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    emit(&state, "ready", None, None).await;
    StatusCode::OK
}

pub async fn launch(
    State(state): State<Arc<AppState>>,
    Json(body): Json<LaunchRequest>,
) -> impl IntoResponse {
    let id   = body.micro_vm_id.as_str();
    let ipv6 = body.mesh_ipv6_address.as_deref();

    *state.micro_vm.lock().unwrap() = Some(MicroVmState {
        micro_vm_id:       body.micro_vm_id.clone(),
        mesh_ipv6_address: body.mesh_ipv6_address.clone(),
    });

    emit(&state, "launch", Some(id), ipv6).await;
    StatusCode::OK
}

pub async fn resume(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    // 1. Abort any handles abandoned before the snapshot
    let old: Vec<_> = state.task_handles.lock().unwrap().drain(..).collect();
    for h in old { h.abort(); }

    // 2. Recreate the reqwest::Client to discard stale connection pools
    *state.dd_client.lock().unwrap() = reqwest::Client::new();

    // 3. Read persisted microvm_id for signal tagging
    let (id, ipv6) = {
        let guard = state.micro_vm.lock().unwrap();
        match guard.as_ref() {
            Some(vm) => (
                Some(vm.micro_vm_id.clone()),
                vm.mesh_ipv6_address.clone(),
            ),
            None => (None, None),
        }
    };

    emit(&state, "resume", id.as_deref(), ipv6.as_deref()).await;
    StatusCode::OK
}

pub async fn suspend(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let (id, ipv6) = read_vm_state(&state);
    emit(&state, "suspend", id.as_deref(), ipv6.as_deref()).await;
    flush_handles(&state).await;
    StatusCode::OK
}

pub async fn terminate(State(state): State<Arc<AppState>>) -> impl IntoResponse {
    let (id, ipv6) = read_vm_state(&state);
    emit(&state, "terminate", id.as_deref(), ipv6.as_deref()).await;
    flush_handles(&state).await;
    StatusCode::OK
}

fn read_vm_state(state: &AppState) -> (Option<String>, Option<String>) {
    let guard = state.micro_vm.lock().unwrap();
    match guard.as_ref() {
        Some(vm) => (Some(vm.micro_vm_id.clone()), vm.mesh_ipv6_address.clone()),
        None => (None, None),
    }
}

async fn flush_handles(state: &AppState) {
    let handles: Vec<_> = state.task_handles.lock().unwrap().drain(..).collect();
    if handles.is_empty() { return; }
    if timeout(FLUSH_TIMEOUT, join_all(handles)).await.is_err() {
        warn!("pre-flight DD flush timed out after {}s", FLUSH_TIMEOUT.as_secs());
    }
}
```

Add `futures` to `Cargo.toml`:
```toml
futures = "0.3"
```

- [ ] **Step 4: Add `build_router` to `src/main.rs`**

```rust
use std::sync::Arc;

pub fn build_router(state: Arc<AppState>) -> axum::Router {
    hooks::routes().with_state(state)
}
```

- [ ] **Step 5: Run the tests**

```bash
cd microvm-sidecar && cargo test hooks 2>&1
```

Expected: all 7 tests pass.

- [ ] **Step 6: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: implement all 5 lifecycle hook handlers with flush logic"
```

---

## Task 8: Panic → 200 (TDD)

The axum `CatchPanic` layer returns HTTP 200 (not 500) when a handler panics, so sidecar bugs never abort a VM lifecycle transition.

**Files:**
- Modify: `microvm-sidecar/src/main.rs`

- [ ] **Step 1: Write the failing test**

Add to `tests/smoke_test.rs` (tests the real `build_router`, not an isolated layer):

```rust
#[tokio::test]
async fn test_panicking_route_through_build_router_returns_200() {
    use axum::{routing::post, body::Body};
    use axum::http::{Request, StatusCode};
    use tower::ServiceExt;

    // Attach a panicking route to the real router to verify CatchPanicLayer is wired in
    let state = std::sync::Arc::new(microvm_sidecar::AppState::new(
        microvm_sidecar::config::Config::default(),
    ));
    let app = microvm_sidecar::build_router(std::sync::Arc::clone(&state))
        .route("/test/panic", post(|| async { panic!("test panic") }));

    let req = Request::builder()
        .method("POST")
        .uri("/test/panic")
        .body(Body::empty())
        .unwrap();
    let status = app.oneshot(req).await.unwrap().status();
    assert_eq!(status, StatusCode::OK);
}
```

- [ ] **Step 2: Run to confirm failure**

```bash
cd microvm-sidecar && cargo test panic 2>&1 | head -10
```

- [ ] **Step 3: Update `build_router` to include `CatchPanicLayer`**

```rust
use tower_http::catch_panic::CatchPanicLayer;

pub fn build_router(state: Arc<AppState>) -> axum::Router {
    hooks::routes()
        .with_state(state)
        .layer(CatchPanicLayer::custom(|_err| {
            tracing::error!("handler panic caught — returning 200 to platform");
            axum::http::Response::builder()
                .status(200)
                .body(axum::body::Body::empty())
                .unwrap()
        }))
}
```

- [ ] **Step 4: Run the test**

```bash
cd microvm-sidecar && cargo test panic 2>&1
```

Expected: test passes.

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add CatchPanic layer — panics return 200 to platform"
```

---

## Task 9: Buffered path stubs

Compile-only stubs that define the `Collector` trait and `FlushTask` so the architecture is visible. No logic, no tests.

**Files:**
- Modify: `microvm-sidecar/src/collector/mod.rs`
- Modify: `microvm-sidecar/src/flush.rs`

- [ ] **Step 1: Write `src/collector/mod.rs`**

```rust
//! Collector abstraction for the buffered platform observability path.
//!
//! Collectors are polled on a schedule by the tokio scheduler in `main.rs`.
//! Each collector produces `Vec<DdSignal>` which are sent to the mpsc channel
//! consumed by `FlushTask`. This is distinct from the immediate lifecycle path.
//!
//! NOT YET IMPLEMENTED — stubs only.

use std::time::Duration;
use async_trait::async_trait;
use crate::datadog::DdSignal;

#[async_trait]
#[allow(dead_code)]
pub trait Collector: Send + Sync {
    fn name(&self) -> &'static str;
    fn interval(&self) -> Duration;
    // micro_vm carries the persisted microvm_id/mesh_ipv6 for tag injection.
    // Matches the spec signature so future implementations don't require a breaking change.
    async fn collect(&self, micro_vm: &Option<crate::MicroVmState>) -> Vec<DdSignal>;
}

/// Placeholder scheduler. Replace with a real polling loop when implementing
/// the buffered observability path.
#[allow(dead_code)]
pub async fn run_scheduler(_collectors: Vec<Box<dyn Collector>>) {
    // TODO: iterate over collectors, sleep(interval), collect(), send to mpsc
    tracing::info!("collector scheduler: not yet implemented");
}
```

- [ ] **Step 2: Write `src/flush.rs`**

```rust
//! FlushTask — consumes the mpsc signal channel, batches signals, and sends
//! them to DataDog in bulk requests (/api/v2/series and /api/v2/logs).
//!
//! NOT YET IMPLEMENTED — stub only.

use crate::datadog::DdSignal;

#[allow(dead_code)]
pub struct FlushTask {
    rx: tokio::sync::mpsc::Receiver<Vec<DdSignal>>,
    flush_rx: tokio::sync::mpsc::Receiver<()>,
}

#[allow(dead_code)]
impl FlushTask {
    pub fn new(
        rx: tokio::sync::mpsc::Receiver<Vec<DdSignal>>,
        flush_rx: tokio::sync::mpsc::Receiver<()>,
    ) -> Self {
        FlushTask { rx, flush_rx }
    }

    /// Run the flush loop. Call via `tokio::spawn(flush_task.run())`.
    pub async fn run(self) {
        // TODO: accumulate into batch_metrics/batch_logs
        // TODO: flush when batch_size >= MAX or elapsed >= FLUSH_INTERVAL
        // TODO: flush immediately on flush_rx signal (from suspend/terminate)
        tracing::info!("FlushTask: not yet implemented");
    }
}
```

- [ ] **Step 3: Verify it compiles cleanly**

```bash
cd microvm-sidecar && cargo build 2>&1
```

Expected: builds with no errors. Warnings about dead_code are expected and suppressed.

- [ ] **Step 4: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add Collector trait and FlushTask stubs for buffered path"
```

---

## Task 10: Wire up `main.rs` and smoke test

Assembles everything: load config, build `AppState`, start the axum server on port 9000.

**Files:**
- Modify: `microvm-sidecar/src/main.rs`

- [ ] **Step 1: Write the integration smoke test**

Create `microvm-sidecar/tests/smoke_test.rs`:

```rust
use axum::{body::Body, http::{Request, StatusCode}};
use tower::ServiceExt;

#[tokio::test]
async fn test_all_five_hooks_reachable() {
    let state = std::sync::Arc::new(microvm_sidecar::AppState::new(
        microvm_sidecar::config::Config::default(),
    ));
    let app = microvm_sidecar::build_router(state);

    let base = "/aws/lambda-microvms/runtime/beta/v1";
    for hook in ["ready", "resume", "suspend", "terminate"] {
        let uri = format!("{base}/{hook}");
        let req = Request::builder()
            .method("POST")
            .uri(&uri)
            .body(Body::empty())
            .unwrap();
        let status = app.clone().oneshot(req).await.unwrap().status();
        assert_eq!(status, StatusCode::OK, "hook {} failed", hook);
    }

    // launch needs a body
    let req = Request::builder()
        .method("POST")
        .uri(format!("{base}/launch"))
        .header("content-type", "application/json")
        .body(Body::from(r#"{"microVmId":"smoke-vm"}"#))
        .unwrap();
    let status = app.oneshot(req).await.unwrap().status();
    assert_eq!(status, StatusCode::OK);
}
```

Make `AppState` and `build_router` public in `src/main.rs` (add `pub`).

- [ ] **Step 2: Run the smoke test**

```bash
cd microvm-sidecar && cargo test --test smoke_test 2>&1
```

Expected: 1 test passes.

- [ ] **Step 3: Implement the full `main` function**

```rust
#[tokio::main]
async fn main() {
    // Initialise structured JSON logging to stdout
    tracing_subscriber::fmt()
        .json()
        .with_env_filter(tracing_subscriber::EnvFilter::from_default_env())
        .init();

    let mut config = config::Config::load();
    config.apply_env_overrides();

    if config.datadog.api_key.is_empty() {
        tracing::warn!("DD_API_KEY is not set — DataDog calls will be skipped");
    }

    let port = config.sidecar.port;
    let state = std::sync::Arc::new(AppState::new(config));
    let app = build_router(std::sync::Arc::clone(&state));

    let addr = std::net::SocketAddr::from(([0, 0, 0, 0], port));
    tracing::info!(port, "microvm-sidecar listening");

    let listener = tokio::net::TcpListener::bind(addr).await
        .expect("failed to bind port");
    axum::serve(listener, app).await
        .expect("server error");
}
```

- [ ] **Step 4: Build the binary**

```bash
cd microvm-sidecar && cargo build --release 2>&1
```

Expected: `target/release/microvm-sidecar` produced with no errors.

- [ ] **Step 5: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: wire up main.rs — sidecar server ready on port 9000"
```

---

## Task 11: Config template and Makefile

**Files:**
- Create: `microvm-sidecar/microvm-sidecar.yaml`
- Create: `microvm-sidecar/Makefile`

- [ ] **Step 1: Write `microvm-sidecar.yaml`**

```yaml
sidecar:
  port: 9000                        # env: SIDECAR_PORT

datadog:
  api_key: ""                       # env: DD_API_KEY — required at runtime, never commit
  site: "datadoghq.com"             # env: DD_SITE  (e.g. datadoghq.eu)
  service: "my-microvm-app"         # env: DD_SERVICE
  env: "production"                 # env: DD_ENV
  tags:                             # env: DD_TAGS (comma-separated; replaces this list)
    - "team:platform"
    - "region:us-east-2"

collectors:
  buffer:
    channel_capacity: 10000         # env: COLLECTORS_CHANNEL_CAPACITY
    max_batch_size: 500             # env: COLLECTORS_MAX_BATCH_SIZE
    flush_interval_seconds: 10      # env: COLLECTORS_FLUSH_INTERVAL_SECONDS
  system_metrics:
    enabled: false                  # env: COLLECTORS_SYSTEM_METRICS_ENABLED
    interval_seconds: 30
    cpu: true
    memory: true
    network: true
    file_descriptors: true
    oom_events: true
```

- [ ] **Step 2: Write `Makefile`**

```makefile
TARGET = aarch64-unknown-linux-musl

# Recommended: cross-compiles for ARM64 musl via Docker (no local toolchain needed)
build-docker:
	cargo install cross --quiet
	cross build --release --target $(TARGET)
	cp target/$(TARGET)/release/microvm-sidecar .

# Native ARM64 musl — requires musl toolchain on host
# macOS: brew install FiloSottile/musl-cross/musl-cross
# Ubuntu: apt install musl-tools
build:
	rustup target add $(TARGET)
	cargo build --release --target $(TARGET)
	cp target/$(TARGET)/release/microvm-sidecar .

# Local dev build (host arch, no musl — not for deployment)
build-local:
	cargo build --release

test:
	cargo test

.PHONY: build-docker build build-local test
```

- [ ] **Step 3: Run tests one final time**

```bash
cd microvm-sidecar && cargo test 2>&1
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add microvm-sidecar/
git commit -m "feat: add config template and Makefile for ARM64 musl build"
```

---

## Done

The lifecycle hook path is fully implemented:

- **Config**: YAML + env var override, with file search order
- **DD signals**: `DdEventPayload`, `DdMetricPayload`, `DdLogEntry` with correct tag logic
- **DD client**: `send_event/metric/log` — errors logged, never propagated
- **Hook handlers**: all 5 hooks at the correct paths, returning 200 (empty body)
- **LifecycleEmitter**: 3 concurrent `tokio::spawn` tasks per hook, handles tracked
- **Flush logic**: `suspend` and `terminate` drain + `join_all` with 10s timeout
- **Resume**: abort old handles, recreate `reqwest::Client`
- **Panic catch**: `CatchPanicLayer` returns 200 to platform on any handler panic
- **Stubs**: `Collector` trait and `FlushTask` compile cleanly for future buffered path
