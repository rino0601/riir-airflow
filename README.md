# riir-airflow

> [!CAUTION]
> This project is very^3 early stages of development.
> Among the things listed in the document, there are far more things that haven't been done than have been done.

Rewrite airflow in rust. it might be help to simplify to operate airflow.

[KOREAN(한국어)](./README.ko.md)

> [!NOTE]
> maintainer's mother tongue is Korean. so Korean README.md is accurate than English.

## Goal

- (nearly) drop-in-replacement for apache-airflow
- simplify architecture
- simplify python dependency
- simplify DAG deploy

## Serve

Install `riir-airflow` and run `riir-airflow serve --native` for the Axum
listener. It serves `/health` and `/show` on loopback. The default wheel has
zero Python runtime dependencies.

Install `riir-airflow[compat]` to enable the default Uvicorn server or to let
Axum delegate unmatched HTTP paths to the same compatibility app. Axum
keeps one HTTP/1 listener and runs the Python app in the same process.
The compatibility app serves riir API docs in both modes and mounts
Airflow UI, API, and authentication at `/airflow` in prod. Uvicorn reloads
the app in dev. Both servers accept `serve --port PORT`. WebSockets and
HTTP/2 are outside native fallback support.

[Architecture and fallback routing](./docs/explanation/architecture.md)
