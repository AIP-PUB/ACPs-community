**[English](README_en.md) | [中文](README.md)**

# ACPs Docs

This directory is the reference documentation entry point for the ACPs project: quick start, tutorials, CLI reference, and development/testing notes.

The **deployment** baseline procedure is: install dependencies → lay down configuration → migrate the database → issue certificates → start processes in order → probe liveness. For the step-by-step commands see [Manual Deployment from an Application Thin Package](tutorials/manual-deploy-from-app-thin-package_en.md). The **automation installation layer** of `acps-infra` (`release/install-packaging`) automates this procedure and adds multi-machine orchestration and idempotent upgrades: application release package → (image) image package → installation package → Ansible. For the concepts see [`acps-infra/README.md`](../acps-infra/README.md); for the step-by-step commands see the tutorials below.

## Directory Structure

```text
acps-docs/
|-- getting-started/
|   `-- README.md
|-- tutorials/
|   |-- agent-development.md
|   |-- aip-sdk-tutorial.md
|   |-- aip-identity-binding-verification.md
|   |-- manual-deploy-from-app-thin-package.md # baseline deployment procedure: manual deployment from a thin package
|   |-- app-release-package-build.md
|   |-- docker-image-packages-from-app-release.md
|   |-- install-package-build.md
|   |-- install-package-ansible-deploy.md
|   |-- install-package-ansible-deploy-3nodes.md
|   |-- install-package-clean-slate.md        # destructive clean slate before acceptance / reinstallation
|   |-- install-package-day2-ops.md            # renewal / trust / upgrade / rollback
|   |-- amp-agent-observability.md            # Agent-side AMP observability
|   |-- oidc-web-app-manual-verification.md
|   `-- oidc-acps-cli-device-login.md
|-- references/
|   `-- cli-reference.md
`-- development/
    `-- development-testing-overview.md
```

## Quick Navigation

### 1 General Guides

- ACPs Quick Start: [getting-started/README_en.md](getting-started/README_en.md)
- ACPs AIP development tutorial: [tutorials/agent-development_en.md](tutorials/agent-development_en.md)
- Integrating AMP observability into an Agent: [tutorials/amp-agent-observability_en.md](tutorials/amp-agent-observability_en.md)
- Deployment baseline procedure — manual deployment from an application thin package (any operating system; the entries below automate it): [tutorials/manual-deploy-from-app-thin-package_en.md](tutorials/manual-deploy-from-app-thin-package_en.md)
- Building an application release package from source (shared by image / host): [tutorials/app-release-package-build_en.md](tutorials/app-release-package-build_en.md)
- Building Docker image packages from an application release package (image only): [tutorials/docker-image-packages-from-app-release_en.md](tutorials/docker-image-packages-from-app-release_en.md)
- Assembling an installation package (`--mode image|host`): [tutorials/install-package-build_en.md](tutorials/install-package-build_en.md)
- Ansible deployment with an installation package (image / host documented separately): [tutorials/install-package-ansible-deploy_en.md](tutorials/install-package-ansible-deploy_en.md)
- Three-business-node Ansible deployment: [tutorials/install-package-ansible-deploy-3nodes_en.md](tutorials/install-package-ansible-deploy-3nodes_en.md)
- Clean slate before acceptance / reinstallation (destructive): [tutorials/install-package-clean-slate_en.md](tutorials/install-package-clean-slate_en.md)
- Day-2 operations after installation (renewal / trust / upgrade / rollback): [tutorials/install-package-day2-ops_en.md](tutorials/install-package-day2-ops_en.md)
- Local single machine on macOS Apple Silicon (**image only**): see [install-package-ansible-deploy_en.md §4.7](tutorials/install-package-ansible-deploy_en.md)
- Single host machine: Rocky 8/9 / Ubuntu 20.04/22.04 business machine + control node over SSH; see assembly §2 and the **〔host〕** notes in the deployment document
- OIDC web application manual verification: [tutorials/oidc-web-app-manual-verification_en.md](tutorials/oidc-web-app-manual-verification_en.md)
- acps-cli OIDC Device login: [tutorials/oidc-acps-cli-device-login_en.md](tutorials/oidc-acps-cli-device-login_en.md)

### 2 CLI Documentation

- CLI reference: [references/cli-reference_en.md](references/cli-reference_en.md)

### 3 Development and Testing Documentation

- ACPs development and testing overview: [development/development-testing-overview_en.md](development/development-testing-overview_en.md)

### 4 SDK Documentation

| Document | Link |
| -- | --- |
| ACPs SDK Agent Identity Code (AIC) | [SDK: AIC DOC](../acps-sdk/acps_sdk/aip/README.md) |
| ACPs SDK Agent Capability Specification (ACS) | [SDK: ACS DOC](../acps-sdk/acps_sdk/acs/README.md) |
| ACPs SDK Agent Discovery Protocol (ADP) | [SDK: ADP DOC](../acps-sdk/acps_sdk/adp/README.md) |
| ACPs Agent Interaction Protocol (AIP) SDK development guide | [tutorials/aip-sdk-tutorial_en.md](tutorials/aip-sdk-tutorial_en.md) |
| How AIP communication prevents identity forgery | [tutorials/aip-identity-binding-verification_en.md](tutorials/aip-identity-binding-verification_en.md) |

## Suggested Reading Order

1. [getting-started/README_en.md](getting-started/README_en.md) — decide between development/testing and deployment
2. Developing Leader / Partner → [tutorials/agent-development_en.md](tutorials/agent-development_en.md)
3. Agent-side AMP observability → [tutorials/amp-agent-observability_en.md](tutorials/amp-agent-observability_en.md)
4. OIDC Web / Device → [oidc-web-app-manual-verification_en.md](tutorials/oidc-web-app-manual-verification_en.md), [oidc-acps-cli-device-login_en.md](tutorials/oidc-acps-cli-device-login_en.md)
5. CLI → [references/cli-reference_en.md](references/cli-reference_en.md)
6. Development and testing → [development/development-testing-overview_en.md](development/development-testing-overview_en.md)
7. **Deployment baseline procedure** → [manual-deploy-from-app-thin-package_en.md](tutorials/manual-deploy-from-app-thin-package_en.md) (install dependencies / lay down configuration / migrate / issue certificates / start processes / probe liveness; the following entries all automate this procedure)
8. Application release package (pre-assemble dependencies into a wheelhouse for offline installation) → [app-release-package-build_en.md](tutorials/app-release-package-build_en.md)
9. **image**: image package → [docker-image-packages-from-app-release_en.md](tutorials/docker-image-packages-from-app-release_en.md) → assemble [install-package-build_en.md](tutorials/install-package-build_en.md) §1
10. **host**: skip the image package → assemble [install-package-build_en.md](tutorials/install-package-build_en.md) §2
11. Ansible deployment → [install-package-ansible-deploy_en.md](tutorials/install-package-ansible-deploy_en.md)
12. Multiple machines → [install-package-ansible-deploy-3nodes_en.md](tutorials/install-package-ansible-deploy-3nodes_en.md) or `hosts-multi.example.yml` inside the package
13. To "install from scratch again" on the same batch of machines → [install-package-clean-slate_en.md](tutorials/install-package-clean-slate_en.md), then run `site.yml`
14. Day-2 operations (renewal / trust / upgrade / rollback) → [install-package-day2-ops_en.md](tutorials/install-package-day2-ops_en.md)
15. Concepts overview → [`acps-infra/README.md`](../acps-infra/README.md)
16. SDK → the SDK / AIP tutorials in the tables above
