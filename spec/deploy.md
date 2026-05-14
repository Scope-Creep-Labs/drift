# Edge Compose Fleet Control Plane — Technical Specification

## Name: Drift Deploy

## Overview

A lightweight edge orchestration platform for managing fleets of Docker Compose-based edge devices.

The system is designed for:

* Outbound-only device connectivity
* Intermittently connected/offline devices
* Docker Compose-native deployments
* Fleet/group-based rollouts
* Desired-state reconciliation
* Low operational complexity
* Kubernetes-free edge management

This platform intentionally avoids Kubernetes and focuses on a simpler appliance/edge-device operational model.

---

# Goals

## Functional Goals

* Deploy Compose applications to fleets of edge devices
* Support offline devices that reconcile when reconnecting
* Manage device groups/tags
* Roll out updates progressively
* Maintain deployment history and auditability
* Support rollback
* Provide device health visibility
* Support multiple Docker registries
* Use outbound HTTPS polling only

## Non-Goals

* Kubernetes orchestration
* Service mesh
* Multi-cluster scheduling
* Dynamic container placement
* CRDs/operators
* SSH-based orchestration
* Real-time command execution

---

# High-Level Architecture

```text
                ┌──────────────────────────────┐
                │       Control Plane          │
                │                              │
                │  API + UI + Postgres         │
                │                              │
                │  Desired State Engine        │
                │  Deployment Coordination     │
                │  Fleet Management            │
                └─────────────┬────────────────┘
                              │
                     HTTPS Polling
                              │
      ┌───────────────────────┼───────────────────────┐
      │                       │                       │
┌─────────────┐      ┌─────────────┐         ┌─────────────┐
│ Edge Node A │      │ Edge Node B │         │ Edge Node C │
│             │      │             │         │             │
│ Edge Agent  │      │ Edge Agent  │         │ Edge Agent  │
│ Docker      │      │ Docker      │         │ Docker      │
│ Compose     │      │ Compose     │         │ Compose     │
└─────────────┘      └─────────────┘         └─────────────┘
```

---

# Core Architectural Principle

The system uses a:

## Desired-State Reconciliation Model

NOT:

```text
Run this command now
```

BUT:

```text
Device X should be running revision Y
```

The edge agent continuously reconciles local state toward desired state.

This model naturally supports:

* Offline devices
* Retry behavior
* Eventual consistency
* Reliable deployments
* Stateless backend APIs

---

# Technology Stack

## Control Plane

| Component            | Recommendation             |
| -------------------- | -------------------------- |
| API Backend          | FastAPI (Python) or Go     |
| Database             | PostgreSQL                 |
| Object Storage       | Cloudflare R2 / S3 / MinIO |
| Frontend             | React / Next.js            |
| Background Jobs      | Simple worker process      |
| Auth                 | JWT/OAuth/OIDC             |
| Deployment Packaging | Docker Compose             |

## Edge Agent

| Component      | Recommendation |
| -------------- | -------------- |
| Agent Language | Go             |
| Local DB       | SQLite         |
| Runtime        | Docker Engine  |
| Orchestration  | Docker Compose |
| Connectivity   | HTTPS Polling  |

---

# Why Docker Compose

Compose provides:

* Human-readable manifests
* Existing ecosystem familiarity
* Simpler operational model than Kubernetes
* Multi-container application packaging
* Easy local debugging
* Easy edge-node execution

Example:

```yaml
services:
  app:
    image: ghcr.io/acme/app@sha256:abcd1234
    restart: unless-stopped

  redis:
    image: redis:7
```

---

# Device Connectivity Model

## Outbound-Only HTTPS Polling

Devices NEVER require inbound ports.

Flow:

```text
Edge Agent -> HTTPS -> Control Plane
```

The agent periodically:

1. Authenticates
2. Sends heartbeat
3. Reports current deployments
4. Retrieves desired state
5. Applies updates if necessary

---

# Polling Flow

## Device Check-In

```http
POST /agent/check-in
```

Example request:

```json
{
  "deviceId": "edge-123",
  "agentVersion": "1.0.0",
  "currentRevisions": {
    "app-a": 11
  },
  "health": {
    "cpu": 23,
    "memory": 41,
    "disk": 68
  }
}
```

Example response:

```json
{
  "deployments": [
    {
      "app": "app-a",
      "targetRevision": 12,
      "bundleUrl": "https://cdn.example.com/bundles/app-a-v12.tar.gz",
      "signature": "abcdef123456"
    }
  ]
}
```

---

# Offline Device Handling

## Example Scenario

Deploy App A to Group X.

Group X contains 10 devices.

### Current State

* 8 devices online
* 2 devices offline

### System Behavior

1. Backend records desired revision for ALL devices
2. Online devices reconcile immediately
3. Offline devices remain pending
4. When offline devices reconnect:

   * they poll
   * receive desired revision
   * reconcile automatically

No special queueing infrastructure is required.

Desired state itself acts as the queue.

---

# Control Plane Data Model

## devices

```text
id
name
status
last_seen
agent_version
created_at
```

## groups

```text
id
name
```

## device_groups

```text
device_id
group_id
```

## apps

```text
id
name
```

## app_revisions

```text
id
app_id
version
compose_yaml
bundle_url
created_at
```

## deployments

```text
id
app_id
revision_id
group_id
status
created_at
```

## deployment_targets

```text
deployment_id
device_id
desired_revision_id
status
attempts
last_error
updated_at
```

---

# Deployment Lifecycle

## Deployment States

```text
pending
in_progress
succeeded
failed
rolled_back
cancelled
```

## Device Target States

```text
pending
pulling
applying
healthy
failed
rollback
```

---

# Edge Agent Responsibilities

The edge agent is responsible for:

* Polling control plane
* Maintaining device identity
* Downloading deployment bundles
* Authenticating to container registry
* Running docker compose
* Reporting health/status
* Maintaining local deployment state
* Rollback handling

---

# Local Edge Agent State

The edge agent maintains local persistent state.

Recommended storage:

```text
/var/lib/edge-agent/
```

Contents:

```text
state.db
registry-creds.json
trusted-keys/
deployments/
logs/
```

---

# Local SQLite Schema

## local_state

```text
app
current_revision
desired_revision
last_attempt
health_status
```

## deployment_history

```text
revision
result
timestamp
logs
```

---

# Registry Authentication

## Recommended Model

Registry credentials are stored locally on the edge device.

The edge agent performs:

```text
docker login
docker pull
docker compose up -d
```

locally.

---

# Registry Credential Strategies

## Option 1 — Shared Fleet Token

Simplest approach.

Each device receives:

```text
registry username/password
```

Pros:

* Easy
* Simple
* Good for internal deployments

Cons:

* Weak per-device isolation

---

## Option 2 — Per-Device Credentials

Each device receives unique credentials.

Pros:

* Better security
* Device revocation
* Better auditing

Cons:

* More operational complexity

---

## Option 3 — Short-Lived Credentials

Control plane issues temporary registry tokens.

Pros:

* Strongest security
* Minimal credential exposure

Cons:

* More infrastructure complexity

---

# Image Deployment Strategy

## Use Immutable Digests

Avoid:

```yaml
image: app:latest
```

Prefer:

```yaml
image: ghcr.io/acme/app@sha256:abcdef123
```

Benefits:

* Immutable deployments
* Reliable rollback
* Deterministic reconciliation
* Easier debugging

---

# Bundle Packaging

Each deployment revision consists of:

```text
compose.yaml
env files
metadata
signatures
```

Packaged as:

```text
tar.gz
```

Stored in:

```text
Cloudflare R2 / S3 / MinIO
```

---

# Security Model

## Device Authentication

Devices authenticate using:

* JWT
* Mutual TLS
* Device certificates
* Provisioned tokens

---

# Manifest Signing

Compose bundles should be signed.

Recommended:

* Cosign
* Ed25519 signatures
* Detached signature files

Edge agents verify signatures before deployment.

---

# Rollback Strategy

Edge agent stores:

```text
current revision
previous revision
```

If health checks fail:

```text
docker compose down
restore previous revision
```

Rollback events are reported to control plane.

---

# Health Checks

Edge agent reports:

* Container status
* Restart counts
* CPU
* Memory
* Disk
* Custom app health endpoints

---

# Fleet Grouping

Devices may belong to multiple groups.

Examples:

```text
west
customer-acme
rev-b
production
staging
```

Deployments target groups.

---

# Progressive Rollouts

Future enhancement.

Example:

```text
5% rollout
wait 30 minutes
25% rollout
full rollout
```

This may later justify:

* DBOS
* Temporal
* Durable workflow engine

NOT required for v1.

---

# Why Not Kubernetes

This system intentionally avoids Kubernetes because:

* Edge devices are often resource constrained
* Operational complexity is unnecessary
* Compose is easier to debug
* Desired-state reconciliation already solves offline synchronization
* Kubernetes adds significant infrastructure overhead

---

# Why Postgres Is Enough Initially

The deployment model is fundamentally:

```text
Eventually consistent desired state
```

This does NOT require:

* Kafka
* RabbitMQ
* NATS
* DBOS
* Temporal

for v1.

Postgres tables themselves act as:

* desired-state store
* deployment queue
* audit log
* reconciliation source

---

# Future Enhancements

## Optional Additions

### Durable Workflow Engine

Potentially:

* DBOS
* Temporal

Useful for:

* staged rollouts
* approvals
* deployment orchestration
* complex retry behavior

---

## Optional Real-Time Connectivity

Potentially:

* WebSockets
* MQTT
* NATS

NOT required initially.

---

## Optional Remote Access

Potentially:

* Tailscale
* Headscale

Useful for:

* debugging
* SSH
* emergency repair

NOT required for deployment orchestration.

---

# Recommended v1 Scope

## Must Have

* Device registration
* HTTPS polling
* Compose deployments
* Fleet groups
* Deployment history
* Offline reconciliation
* Rollback
* Health reporting

## Avoid Initially

* Real-time orchestration
* Multi-region replication
* Streaming logs
* Shell access
* Kubernetes support
* Workflow engines
* Multi-cloud abstraction

---

# Recommended Initial Stack

## Backend

```text
FastAPI
Postgres
Worker process
Cloudflare R2
React UI
```

## Edge

```text
Go agent
SQLite
Docker Engine
Docker Compose
HTTPS polling
```

## Frontend
The user will interact with this using the same prompt-based ui. So we will need a route /deploy with a prompt and response. 
Sample prompts:
- "Deploy application A to all devices in group X"
- "Commision a new device under group X"
    - response: a docker run command with all the right env params that user can use to deploy the edge agent
- "Create a new group Z"
- "Create new app B"
    - response: A docker compose editor embedded in the ui with a .env text field

---

# Key Architectural Insight

The system should think in terms of:

```text
Desired State
```

NOT:

```text
Remote Command Execution
```

This single architectural decision simplifies:

* offline handling
* retries
* scalability
* reconciliation
* rollback
* operational reliability

and eliminates the need for most traditional orchestration infrastructure.

