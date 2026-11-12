#!/usr/bin/env bash
# ==============================================================================
# Zero-Edit GCP Setup for GitHub Actions -> Cloud Run (Run in Google Cloud Shell)
# Automatically creates a GCP project (if none exists), links billing, enables
# APIs, creates Artifact Registry + Service Account, and prints GitHub Secrets.
# ==============================================================================
set -euo pipefail

REGION="asia-south1"
REPO_NAME="chasezero-repo"
SA_NAME="github-actions-deployer"

# 1. Detect existing active project or create a new one automatically
PROJECT_ID=$(gcloud projects list --filter="lifecycleState:ACTIVE" --format="value(projectId)" --limit=1 --quiet || true)

if [ -z "${PROJECT_ID}" ]; then
  PROJECT_ID="chasezero-kyc-$((10000 + RANDOM % 90000))"
  echo "👉 No existing GCP project found. Creating new project: ${PROJECT_ID}..."
  gcloud projects create "${PROJECT_ID}" --name="ChaseZero KYC Platform" --quiet
else
  echo "👉 Found existing GCP project: ${PROJECT_ID}"
fi

gcloud config set project "${PROJECT_ID}" --quiet
SA_EMAIL="${SA_NAME}@${PROJECT_ID}.iam.gserviceaccount.com"

# 2. Link Open Billing Account (required by Cloud Run & Artifact Registry)
BILLING_ID=$(gcloud billing accounts list --filter="open=true" --format="value(ACCOUNT_ID)" --limit=1 --quiet || true)
if [ -z "${BILLING_ID}" ]; then
  echo "⚠️  ERROR: No active Google Cloud Billing Account found!"
  echo "   Please activate your Free Trial / Billing Account at:"
  echo "   https://console.cloud.google.com/billing"
  echo "   Then re-run this script."
  exit 1
fi

echo "👉 Linking Billing Account (${BILLING_ID}) to project ${PROJECT_ID}..."
gcloud billing projects link "${PROJECT_ID}" --billing-account="${BILLING_ID}" --quiet

# 3. Enable Required APIs
echo "👉 Enabling Cloud Run, Artifact Registry, Cloud Build, and IAM APIs..."
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  iam.googleapis.com \
  --project="${PROJECT_ID}" \
  --quiet

# 4. Create Docker Artifact Registry Repository (if not exists)
echo "👉 Ensuring Artifact Registry repository '${REPO_NAME}' exists in ${REGION}..."
gcloud artifacts repositories describe "${REPO_NAME}" --location="${REGION}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || \
  gcloud artifacts repositories create "${REPO_NAME}" \
    --repository-format=docker \
    --location="${REGION}" \
    --description="Docker repository for ChaseZero KYC Platform" \
    --project="${PROJECT_ID}" \
    --quiet

# 5. Create Service Account for GitHub Actions (if not exists)
echo "👉 Ensuring Service Account '${SA_EMAIL}' exists..."
gcloud iam service-accounts describe "${SA_EMAIL}" --project="${PROJECT_ID}" --quiet >/dev/null 2>&1 || \
  gcloud iam service-accounts create "${SA_NAME}" \
    --display-name="GitHub Actions Cloud Run Deployer" \
    --project="${PROJECT_ID}" \
    --quiet

# 6. Grant Required IAM Roles
echo "👉 Granting IAM roles to Service Account..."
for ROLE in \
  "roles/run.admin" \
  "roles/artifactregistry.writer" \
  "roles/iam.serviceAccountUser" \
  "roles/storage.admin"; do
  gcloud projects add-iam-policy-binding "${PROJECT_ID}" \
    --member="serviceAccount:${SA_EMAIL}" \
    --role="${ROLE}" \
    --quiet >/dev/null
done

# 7. Generate JSON Key for GitHub Secrets
rm -f ~/gcp-sa-key.json
gcloud iam service-accounts keys create ~/gcp-sa-key.json \
  --iam-account="${SA_EMAIL}" \
  --project="${PROJECT_ID}" \
  --quiet

echo ""
echo "========================================================================"
echo "✅ GCP Setup Complete!"
echo "Add these 2 Repository Secrets in GitHub (Settings -> Secrets and variables -> Actions):"
echo ""
echo "1. Secret Name:  GCP_PROJECT_ID"
echo "   Secret Value: ${PROJECT_ID}"
echo ""
echo "2. Secret Name:  GCP_SA_KEY"
echo "   Secret Value: (Copy the JSON below from { to })"
echo "========================================================================"
cat ~/gcp-sa-key.json
echo "========================================================================"
