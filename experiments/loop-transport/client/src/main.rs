//! Pure-reqwest load generator mirroring perf.py's pool: `concurrency` tasks each
//! take the next request index as soon as their previous request finishes.
use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

#[derive(Clone, Copy, PartialEq)]
enum Body {
    Bytes,
    Json,
}

struct Args {
    url: String,
    runtime: String,
    concurrency: usize,
    requests: usize,
    warmup: usize,
    runs: usize,
    body: Body,
}

fn parse_args() -> Args {
    let mut a = Args {
        url: "http://json-server:3000/".into(),
        runtime: "multi".into(),
        concurrency: 1,
        requests: 20_000,
        warmup: 1_000,
        runs: 5,
        body: Body::Json,
    };
    let argv: Vec<String> = std::env::args().skip(1).collect();
    for pair in argv.chunks(2) {
        let v = pair[1].clone();
        match pair[0].as_str() {
            "--url" => a.url = v,
            "--runtime" => a.runtime = v,
            "--concurrency" => a.concurrency = v.parse().unwrap(),
            "--requests" => a.requests = v.parse().unwrap(),
            "--warmup" => a.warmup = v.parse().unwrap(),
            "--runs" => a.runs = v.parse().unwrap(),
            "--body" => a.body = if v == "bytes" { Body::Bytes } else { Body::Json },
            other => panic!("unknown arg {other}"),
        }
    }
    a
}

fn cpu_seconds() -> f64 {
    let mut ru: libc::rusage = unsafe { std::mem::zeroed() };
    unsafe { libc::getrusage(libc::RUSAGE_SELF, &mut ru) };
    let tv = |t: libc::timeval| t.tv_sec as f64 + t.tv_usec as f64 / 1e6;
    tv(ru.ru_utime) + tv(ru.ru_stime)
}

async fn fetch(client: &reqwest::Client, url: &str, body: Body) {
    let resp = client.get(url).send().await.unwrap();
    assert!(resp.status().is_success());
    let bytes = resp.bytes().await.unwrap();
    if body == Body::Json {
        let v: serde_json::Value = serde_json::from_slice(&bytes).unwrap();
        assert!(v.get("status").is_some());
    } else {
        assert!(!bytes.is_empty());
    }
}

async fn run_pool(client: &reqwest::Client, a: &Args, total: usize) -> Vec<Duration> {
    let next = Arc::new(AtomicUsize::new(0));
    let mut handles = Vec::with_capacity(a.concurrency);
    for _ in 0..a.concurrency {
        let (client, url, next, body) = (client.clone(), a.url.clone(), next.clone(), a.body);
        handles.push(tokio::spawn(async move {
            let mut lat = Vec::new();
            while next.fetch_add(1, Ordering::Relaxed) < total {
                let t = Instant::now();
                fetch(&client, &url, body).await;
                lat.push(t.elapsed());
            }
            lat
        }));
    }
    let mut all = Vec::with_capacity(total);
    for h in handles {
        all.extend(h.await.unwrap());
    }
    all
}

fn pct(sorted: &[Duration], p: f64) -> f64 {
    let i = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[i].as_secs_f64() * 1e6
}

async fn bench(a: Args) {
    let client = reqwest::Client::builder().build().unwrap();
    run_pool(&client, &a, a.warmup).await;
    let body = if a.body == Body::Json { "json" } else { "bytes" };
    for run in 0..a.runs {
        let cpu0 = cpu_seconds();
        let t0 = Instant::now();
        let mut lat = run_pool(&client, &a, a.requests).await;
        let wall = t0.elapsed().as_secs_f64();
        let cpu = cpu_seconds() - cpu0;
        lat.sort();
        println!(
            "{{\"impl\":\"reqwest\",\"runtime\":\"{}\",\"body\":\"{}\",\"concurrency\":{},\"run\":{},\"requests\":{},\"rps\":{:.0},\"cpu_us_per_req\":{:.2},\"p50_us\":{:.1},\"p99_us\":{:.1},\"max_us\":{:.1}}}",
            a.runtime, body, a.concurrency, run, a.requests,
            a.requests as f64 / wall, cpu / a.requests as f64 * 1e6,
            pct(&lat, 0.50), pct(&lat, 0.99), pct(&lat, 1.0)
        );
    }
}

fn main() {
    let a = parse_args();
    let rt = match a.runtime.as_str() {
        "current" => tokio::runtime::Builder::new_current_thread().enable_all().build(),
        _ => tokio::runtime::Builder::new_multi_thread().enable_all().build(),
    }
    .unwrap();
    rt.block_on(bench(a));
}
