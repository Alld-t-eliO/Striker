# Striker

**A modular offensive security toolkit for controlled DoS resilience testing and stealth HTTP operations.**

Striker is a Python-based framework designed to test the resilience of web services against Denial-of-Service conditions in **isolated lab environments**, and to perform furtive HTTP operations when interacting with rate-limited or protected endpoints.

It ships with a terminal UI (TUI) for interactive control, a CLI for automation, and a modular architecture that separates offensive, defensive-adjacent, and helper concerns.

---

## Table of Contents

- [Purpose](#purpose)
- [Features](#features)
- [Architecture](#architecture)
- [Installation](#installation)
- [Usage](#usage)
  - [Terminal UI](#terminal-ui)
  - [Command Line](#command-line)
- [Modules](#modules)
- [Legal & Ethical Framework](#legal--ethical-framework)
- [Roadmap](#roadmap)
- [Contributing](#contributing)
- [License](#license)

---

## Purpose

Striker exists to answer a specific operational question:

> *"How does my infrastructure behave under stress, and where are my defenses actually effective?"*

The toolkit is built around three pillars:

1. **Resilience testing** — controlled slowloris and connection-exhaustion scenarios against services you own, on isolated networks, to validate timeouts, worker pools, and rate-limiting configurations.
2. **Stealth HTTP operations** — backoff, jitter, proxy rotation, user-agent rotation, and CAPTCHA/WAF handling for legitimate interactions with endpoints that defend themselves aggressively.
3. **Observability** — every module exposes structured state (sockets alive, workers active, error rates, circuit-breaker status) so results can be measured, compared, and reported.

Striker is **not** a weaponized DDoS platform. It does not ship with botnet capability, amplification primitives, or public-target automation. Its offensive module is a **lab instrument**, not a service.

---

## Features

### Offensive module
- Multi-threaded slowloris with automatic socket renewal
- Configurable threads, sockets per thread, and keep-alive intervals
- Clean start / stop API, safe for interactive use
- Live metrics: alive sockets, opened, closed, threads active, uptime
- **Private-IP guard** enforced at the code level for any automated entry point

### Stealth HTTP module
- Adaptive backoff with four strategies: `aimd`, `pid`, `simple`, `adaptive`
- Circuit breaker per domain with cooldown and auto-recovery
- Retry-After parsing (integer seconds and HTTP-date formats)
- Suspect-page detection (CAPTCHA, block pages, WAF interstitials)
- Proxy rotation with TTL-based rehabilitation and permanent-ban distinction
- User-agent rotation with coherent browser headers
- Delay jitter with multiple statistical profiles: `uniform`, `gaussian`, `exponential`, `lognormal`, `human`
- Thread-safe rate limiter (sliding window)
- TLS impersonation via `curl_cffi` (JA3 fingerprint alignment)
- CAPTCHA / WAF bypass: detection, API solving, browser fallback
- Request fragmentation for chunked uploads (requires a cooperating server)

### Orchestration
- Thread-based worker pool with per-domain sharding
- Optional Redis-backed task queue for distributed setups
- Signal handling for graceful shutdown
- Structured statistics returned at end of run

### Interface
- Textual-based TUI with cyan / violet / green / red / yellow palette
- Live status panel refreshed every 500 ms
- Keyboard shortcuts and button controls
- Dedicated layout for the offensive module

---

## Installation

### Requirements

- Python **3.10+** (uses modern type hints and dataclasses)
- A Unix-like environment recommended (Linux, macOS)

### Setup

```bash
# Clone or copy the project
cd striker

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate          # Linux / macOS
# venv\Scripts\activate           # Windows

# Install core dependencies
pip install -r requirements.txt

# (Optional) Install the browser engine for CAPTCHA fallback
playwright install chromium
