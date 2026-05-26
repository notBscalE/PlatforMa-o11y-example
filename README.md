# PlatforMa Demo — Agentic Platform Orchestration on AWS EKS

An example project showing how to run **AI agents for autonomous incident detection, diagnosis, and remediation** on a Kubernetes platform. Built as a reference for teams exploring an agentic approach to platform operations.

The agents use [Claude](https://anthropic.com) (claude-opus-4-7) with tool-use to query live system state — Prometheus, ArgoCD, Kubernetes, and CloudWatch — reason about what's wrong, and either fix it automatically or walk you through the fix interactively via GitHub Issues.

The example was co-written by Claude Code. You can explore your options by either using this example, using this repo as a starting point to continue developing a solution that suits you more, or even use Claude Code (or any other vibe-coding tool) to start your own solution from the ground up.

This is a clean-slate project - the code in this repo was used in the session, but the repo itself wasn't - so there are not any records of PRs and issues in this repository.

---

## What's in this repo

| Layer | Technology | What it does |
|---|---|---|
| **App** | Go + Docker | Simple HTTP service with `/health` endpoint |
| **Infra** | Terraform + AWS EKS | VPC, EKS cluster, node groups, IAM, networking |
| **GitOps** | ArgoCD | Syncs Helm charts from this repo to the cluster |
| **Monitoring** | Prometheus + Grafana | Metrics, alerts, dashboards |
| **Agents** | Python + Claude API | Autonomous incident response (see below) |
| **CI/CD** | GitHub Actions | Test, build, deploy on every push |

---

## Agentic incident response

The core of this project is a multi-agent system that handles the full incident lifecycle:

```
Alert fires (Prometheus AlertManager)
      │
      ▼
┌─────────────────────────────────────────────────────┐
│                    Orchestrator                     │
│                                                     │
│  1. Opens a GitHub Issue                            │
│  2. DiagnosticAgent investigates                    │
│     ├── queries Prometheus metrics                  │
│     ├── checks ArgoCD app health                    │
│     ├── reads kubectl pod logs & events             │
│     └── searches CloudWatch for cluster errors      │
│  3. Classifies: infrastructure / code / transient   │
│  4. Acts based on category (see below)              │
└─────────────────────────────────────────────────────┘
```

### Incident categories and actions

**Transient** (spike already resolving)
→ Posts a comment explaining the situation, closes the issue. No action needed.

**Code bug** (application fault)
→ Applies immediate kubectl actions (rollback, scale) to reduce blast radius  
→ Creates a new branch with the code fix  
→ Opens a PR — CI tests it, DevOps reviews and merges  
→ When system is healthy: commits an incident report to `incidents/`, closes the issue

**Infrastructure** (cluster, node, network, resources)
→ Applies immediate kubectl actions to reduce blast radius  
→ Asks for DevOps approval by adding `awaiting-approval` label to the GitHub Issue  
→ DevOps adds the `approved` label → agent creates a Terraform/Helm fix PR  
→ When system is healthy: commits an incident report to `incidents/`, closes the issue

### Talking to the agents

The agents respond to **any GitHub Issue on this repo** — both ones they open themselves and ones you open manually.

Open an issue like *"Why are pods restarting?"* or *"Show me memory usage for the last hour"* and within 30 seconds the agent replies with data from the live cluster. Follow-up comments continue the conversation with full history.

The agent has the same diagnostic tools in every thread: Prometheus PromQL, ArgoCD API, `kubectl` (pods, logs, events, nodes), and CloudWatch Insights.

### Triggers

The agents wake up from three sources:

| Source | Mechanism | Description |
|---|---|---|
| **AlertManager** | Webhook → `POST /webhook/alertmanager` | Fires when a Prometheus alert is critical |
| **ArgoCD** | Webhook → `POST /webhook/argocd` | Fires when an app goes Degraded or Missing |
| **CronJob** | `GET /check` every 5 minutes | Polls ArgoCD + Prometheus + pod readiness |

### Prometheus alert rules

| Alert | Condition | Severity |
|---|---|---|
| `PlatformmaAppDown` | No healthy Prometheus targets | critical |
| `PlatformmaHighRestarts` | >3 pod restarts in 15 min | high |
| `PlatformmaPodsNotReady` | Ready replicas < desired | high |
| `PlatformmaNodeNotReady` | Node NotReady >2 min | critical |

---

## Repository structure

```
.
├── app/                        Go HTTP service
│   ├── main.go
│   └── Dockerfile
│
├── agents/                     Observability agents (Python/FastAPI)
│   ├── Dockerfile
│   ├── requirements.txt
│   ├── pytest.ini
│   └── src/
│       ├── main.py             FastAPI server (webhook receiver)
│       ├── orchestrator.py     Incident lifecycle state machine
│       ├── architecture.yaml   Static platform knowledge
│       ├── agents/
│       │   ├── chat_agent.py       GitHub Issue conversation handler
│       │   ├── diagnostic.py       Claude tool-use investigation loop
│       │   ├── health_checker.py   Post-fix health verification
│       │   └── remediation.py      Branch/PR/incident-doc creation
│       └── tools/
│           ├── prometheus.py   PromQL HTTP API client
│           ├── argocd.py       ArgoCD REST API client
│           ├── cloudwatch.py   AWS CloudWatch Logs (boto3)
│           ├── kubectl.py      Kubernetes Python client
│           └── github.py       PyGithub wrapper
│
├── helm/
│   ├── platformma-app/         Helm chart for the Go app
│   └── observability-agents/   Helm chart for the agents
│       └── templates/
│           ├── prometheusrule.yaml   Alert definitions
│           ├── clusterrole.yaml      RBAC for kubectl access
│           └── ...
│
├── terraform/
│   ├── bootstrap/              S3 state bucket (run once)
│   └── infra/
│       ├── eks.tf              EKS cluster + node groups
│       ├── vpc.tf              VPC, subnets, IGW, route tables
│       ├── security-groups.tf  Firewall rules
│       ├── argocd.tf           ArgoCD Helm release + Applications
│       ├── prometheus.tf       kube-prometheus-stack + AlertManager config
│       ├── observability-agents.tf   IRSA role, namespace, k8s secret
│       ├── github-actions-iam.tf     OIDC role for CI/CD
│       └── variables.tf
│
├── incidents/                  Auto-generated incident reports (markdown)
└── github-ci-workflow/
    └── pipeline.yml            CI/CD pipeline (not saved in .github/workflows to avoid
                                unattended CI triggering - Change after fork
```

---

## CI/CD pipeline

Every push to `main` or pull request runs the relevant checks:

```
push / PR
    │
    ├── changes          detect which directories changed
    │
    ├── test-helm        helm lint (both charts)         if helm/** changed
    ├── test-terraform   fmt-check + validate + plan      if terraform/** changed
    └── test-agents      ruff lint + pytest (13 tests)   if agents/** changed
         │
         └── (on main only, tests passed)
              │
              ├── build-and-push          Docker → ghcr.io
              ├── build-and-push-agents   Docker → ghcr.io
              └── terraform-apply
                    ├── terraform apply
                    └── kubectl rollout restart (agents, if agents/ changed)
```

GitHub Actions authenticates to AWS via **OIDC** — no stored credentials.

---

## Infrastructure design

**Cost-optimised choices:**
- Nodes in **public subnets** — no NAT gateway (~$33/month saved)
- **SPOT instances** (`t3.medium`) — ~70% cheaper than on-demand
- 7-day Prometheus retention, 10 GiB EBS
- Terraform state in S3 only (no DynamoDB)

**Estimated monthly cost:** ~$110–130 (2× t3.medium spot + EKS control plane + LoadBalancers + EBS)

**Security:**
- SSH and admin ports (Prometheus, Grafana) restricted to `my_ip` only
- App on port 80 open publicly
- GitHub Actions uses OIDC (no long-lived AWS credentials)
- Agent secrets (`ANTHROPIC_API_KEY`, `GITHUB_TOKEN`) stored as GitHub Actions encrypted secrets, written to a Kubernetes Secret by Terraform — never in code or state output

---

## Getting started

### Prerequisites

- AWS CLI configured
- Terraform ≥ 1.6
- `kubectl`, `helm`
- GitHub account (for GHCR image registry and GitHub Actions)
- Anthropic API key (for the agents) — [console.anthropic.com](https://console.anthropic.com)

### 1. Fork and clone

Fork this repo, then clone your fork. The rest of the setup targets your fork's GitHub Actions and GHCR.

### 2. Bootstrap — create S3 state bucket

```bash
cd terraform/bootstrap
terraform init
terraform apply
```

Run once. Creates the `platformma-terraform-state` S3 bucket.

### 3. Configure Terraform variables

```bash
cd terraform/infra
cp terraform.tfvars.example terraform.tfvars
```

Edit `terraform.tfvars`:
```hcl
my_ip      = "1.2.3.4/32"   # curl -s https://checkip.amazonaws.com/
github_repo = "https://github.com/YOUR_ORG/EXAMPLE_REPO.git"
```

### 4. Set GitHub Actions secrets

In your repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|---|---|
| `TF_VAR_MY_IP` | Your IP in CIDR format (`1.2.3.4/32`) |
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |
| `AGENTS_GITHUB_TOKEN` | GitHub PAT with `repo` + `issues` scope |

And one GitHub Actions **variable** (not secret):

| Variable | Value |
|---|---|
| `AWS_ROLE_ARN` | Set after step 5 — see below |

### 5. Deploy infrastructure

```bash
cd terraform/infra
terraform init
terraform apply   # ~15 min
```

After apply, copy the `github_actions_role_arn` output into the `AWS_ROLE_ARN` GitHub Actions variable. Future applies run through CI.

### 6. Enable EKS API auth mode (one-time)

```bash
aws eks update-cluster-config \
  --name platformma \
  --access-config authenticationMode=API_AND_CONFIG_MAP
```

### 7. Verify

```bash
aws eks update-kubeconfig --region us-east-1 --name platformma

# App
APP=$(kubectl get svc -n platformma platformma \
  -o jsonpath='{.status.loadBalancer.ingress[0].hostname}')
curl http://$APP/health
# {"status":"alive","time":"..."}

# Agents
kubectl get pods -n observability-agents
```

### 8. Create the GHCR imagePullSecret (private image)

```bash
kubectl create secret docker-registry ghcr-pull-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password=YOUR_PAT \
  -n platformma

kubectl create secret docker-registry ghcr-pull-secret \
  --docker-server=ghcr.io \
  --docker-username=YOUR_GITHUB_USERNAME \
  --docker-password=YOUR_PAT \
  -n observability-agents
```

---

## Services

| Service | How to find the address | Port | Access |
|---|---|---|---|
| App | `kubectl get svc -n platformma` | 80 | Public |
| ArgoCD | `kubectl get svc -n argocd argocd-server` | 80 | Public |
| Prometheus | `kubectl get svc -n monitoring kube-prometheus-stack-prometheus` | 9090 | `my_ip` only |
| Grafana | `kubectl get svc -n monitoring kube-prometheus-stack-grafana` | 80 | `my_ip` only |
| Agents | ClusterIP — not exposed externally | 8080 | In-cluster |

Grafana default credentials: `admin` / value of `grafana_admin_password` in `terraform.tfvars`

---

## Interacting with the agents

**Automated:** AlertManager and ArgoCD trigger the agents automatically when something goes wrong.

**Manual:** Open any GitHub Issue on this repo. The agent will reply within 30 seconds with data from the live cluster. Examples:

- *"Why are pods restarting?"*
- *"Show me memory usage for the last hour"*
- *"Is the cluster healthy?"*
- *"Describe the last deployment"*

During an active incident, comment on the agent-created issue to ask questions, request more data, or instruct it to take an action.

---

## Tear down

```bash
cd terraform/infra && terraform destroy

# The S3 state bucket has prevent_destroy = true — delete manually if needed:
aws s3 rb s3://platformma-terraform-state --force
```

---

## Key design decisions

**Why GitHub Issues for incident interaction?**
No extra infrastructure. Every engineer already has GitHub open. The conversation is searchable, auditable, and linked to the PR that fixes the issue.

**Why not auto-apply infrastructure fixes without approval?**
Infra changes have blast radius. The agent applies immediate kubectl mitigations (restart, scale) automatically, but permanent Terraform changes require a human `approved` label. The PR then goes through normal CI before merging.

**Why SPOT instances with no NAT?**
This is a demonstration project. SPOT saves ~70%, and running nodes in public subnets (with restrictive security groups) avoids the $33/month NAT gateway. For production, private subnets + NAT are recommended.

**Why Terraform for ArgoCD app registration?**
The ArgoCD Application manifests live alongside the infrastructure that creates the cluster, making `terraform apply` a single command that produces a fully operational platform including GitOps wiring.
