# Security Policy

## Supported Versions

| Version | Supported |
|---------|-----------|
| 0.1.x   | ✅ Yes     |

## Reporting a Vulnerability

If you discover a security vulnerability in this project, please **do not**
open a public GitHub issue.

Instead, email us privately at **nlp@fit.hcmus.edu.vn** with:

1. A description of the vulnerability
2. Steps to reproduce it
3. Potential impact assessment
4. Any suggested mitigation (optional)

We will acknowledge your report within **48 hours** and aim to resolve confirmed
vulnerabilities within **14 days**.

## Scope

This project processes historical text data and runs local HTTP scraping.
Security concerns most relevant to this codebase include:

- Unsafe deserialisation of downloaded data
- SSRF (Server-Side Request Forgery) via scraper URL configuration
- Path traversal in file-saving logic
- Dependency vulnerabilities in third-party packages

## Out of Scope

- Vulnerabilities in Jupyter/Colab infrastructure itself
- Issues in the Nom Foundation website we scrape from
