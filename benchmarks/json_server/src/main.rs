use axum::{Json, Router};
use serde_json::json;
use std::net::SocketAddr;

#[tokio::main]
async fn main() {
    let app = Router::new().route("/", axum::routing::get(handler));

    let addr = SocketAddr::from(([0, 0, 0, 0], 3000));
    println!("Server running at http://{}", addr);

    let listener = tokio::net::TcpListener::bind(&addr).await.unwrap();
    axum::serve(listener, app).await.unwrap();
}

async fn handler() -> Json<serde_json::Value> {
    Json(json!({
        "status": "success",
        "timestamp": "2026-08-19T12:34:56Z",
        "user": {
            "id": 42,
            "name": "Alice Johnson",
            "email": "alice@example.com",
            "roles": ["admin", "editor"],
            "metadata": {
                "last_login": "2026-08-19T10:00:00Z",
                "login_count": 127,
                "active": true
            }
        },
        "data": [
            {
                "id": 1,
                "title": "First Item",
                "tags": ["rust", "async"],
                "score": 95.5,
                "nested": {
                    "depth": 2,
                    "values": [10, 20, 30]
                }
            },
            {
                "id": 2,
                "title": "Second Item",
                "tags": ["web", "performance"],
                "score": 88.3,
                "nested": {
                    "depth": 2,
                    "values": [40, 50, 60]
                }
            }
        ],
        "pagination": {
            "page": 1,
            "limit": 2,
            "total": 42,
            "has_more": true
        }
    }))
}
