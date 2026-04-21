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
    #[serde(default = "default_channel_capacity")]
    pub channel_capacity: usize,
    #[serde(default = "default_max_batch_size")]
    pub max_batch_size: usize,
    #[serde(default = "default_flush_interval")]
    pub flush_interval_seconds: u64,
}

fn default_channel_capacity() -> usize { 10000 }
fn default_max_batch_size() -> usize { 500 }
fn default_flush_interval() -> u64 { 10 }

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
            .unwrap_or_else(|e| panic!("failed to read config file {}: {}", p.display(), e));
        serde_yml::from_str(&content)
            .unwrap_or_else(|e| panic!("failed to parse config YAML in {}: {}", p.display(), e))
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
        let result = std::panic::catch_unwind(|| {
            assert_eq!(cfg.datadog.api_key, "env-key");
        });
        std::env::remove_var("DD_API_KEY");
        result.unwrap();
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
        let result = std::panic::catch_unwind(|| {
            assert_eq!(cfg.datadog.tags, vec!["team:infra", "region:us-east-2"]);
        });
        std::env::remove_var("DD_TAGS");
        result.unwrap();
    }
}
