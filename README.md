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
listener. It serves `/health` and `/show` on loopback without an Airflow or
FastAPI dependency.

Install `riir-airflow[compat]` and run `riir-airflow serve` for the default
Uvicorn/ASGI server with the existing Airflow integrations. Dev mode reloads
the ASGI app, and prod mode mounts Airflow at `/airflow` in an initialized
Airflow environment. Both servers accept `serve --port PORT`.
