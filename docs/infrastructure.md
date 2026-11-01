# Infrastructure (Terraform)

Everything the app needs on AWS is defined in `infrastructure/`. One `terraform apply` creates a complete, separate environment (`docassist-dev`); one `terraform destroy` removes it.

| File | What it defines |
|---|---|
| `storage.tf` | DynamoDB table (with owner index and TTL), private S3 bucket (encryption, CORS, HTTPS-only policy), S3 trigger for the PDF processor |
| `lambda.tf` | Both Lambda functions, their log groups and least-privilege IAM roles |
| `auth.tf` | Cognito user pool, hosted login domain, web client (Authorization Code + PKCE) |
| `api.tf` | HTTP API with JWT authorizer, CORS, throttling and access logs |
| `monitoring.tf` | SNS alert topic, error metric filter, 5 alarms, dashboard |
| `outputs.tf` | URLs and IDs; writes `frontend/.env.local` for the frontend |

## Import vs. a new environment

There are two ways to bring resources that were clicked together in the console under Terraform:

- **Import**: describe each existing resource in code and run `terraform import` (or `import` blocks) so Terraform adopts it. Nothing is recreated, but every attribute has to match the real resource exactly, or the next `apply` changes or replaces it. Risky for a working app.
- **New environment** (used here): Terraform creates a fresh copy with its own names. The console-built version keeps working, and the code proves it can rebuild everything from zero. Once the new environment works, the old one can be deleted by hand.

## One-time setup

### 1. Tools

- [Terraform](https://developer.hashicorp.com/terraform/install) 1.10 or newer: `terraform -version`
- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html): `aws --version`
- Python 3.12 and Node.js 20+

### 2. AWS credentials (not the root user)

Never create access keys for the root user. Create a separate user for Terraform:

1. IAM → Users → Create user `terraform-admin` (no console access).
2. Attach the policy `AdministratorAccess` directly.
3. User → Security credentials → Create access key → *Command Line Interface (CLI)*.
4. On your machine: `aws configure` → paste the key ID and secret, region `eu-north-1`, output `json`.
5. Check: `aws sts get-caller-identity` shows `user/terraform-admin`.

Once CI/CD deploys through GitHub OIDC (Phase 9), this key can be deactivated.

### 3. State bucket

Terraform records what it created in a state file. It is kept in S3 so it survives your laptop and can be shared with CI. Create the bucket once (replace `ACCOUNT_ID`):

```bash
aws s3api create-bucket --bucket docassist-tfstate-ACCOUNT_ID --region eu-north-1 \
  --create-bucket-configuration LocationConstraint=eu-north-1
aws s3api put-bucket-versioning --bucket docassist-tfstate-ACCOUNT_ID \
  --versioning-configuration Status=Enabled
aws s3api put-public-access-block --bucket docassist-tfstate-ACCOUNT_ID \
  --public-access-block-configuration BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
```

### 4. Config files

```bash
cd infrastructure
cp backend.hcl.example backend.hcl            # put your account ID in the bucket name
cp terraform.tfvars.example terraform.tfvars  # put your email in alert_email
```

Both files are ignored by git.

## Deploy

From the repository root:

```bash
python scripts/build.py          # packages the PDF processor with pypdf into .build/
cd infrastructure
terraform init -backend-config=backend.hcl
terraform validate
terraform plan                   # read it: everything should be "create", nothing "destroy"
terraform apply                  # type "yes"
```

`apply` takes 1–2 minutes and prints the outputs. It also writes `frontend/.env.local`.

Then:

1. Confirm the "AWS Notification - Subscription Confirmation" email so alarms can reach you.
2. Create your login with the command from the `create_user_command` output (replace `YOUR_EMAIL` twice). Cognito emails a temporary password; you choose a new one at first sign-in.
3. Start the frontend:

   ```bash
   cd frontend
   npm install
   npm run dev
   ```

4. Open http://localhost:3000, sign in, upload a PDF.

## Change something

Edit the code or `.tf` files, then run `python scripts/build.py` and `terraform apply` again. Terraform only changes what differs.

## Remove everything

```bash
cd infrastructure
terraform destroy
```

This deletes the dev environment including uploaded PDFs (the bucket has `force_destroy` outside prod). The state bucket stays.

## Cost when idle

Nothing in this stack charges per hour. Lambda, API Gateway, DynamoDB (on-demand), S3, Cognito, SNS and the 5 alarms and one dashboard stay at or near zero for a demo. Logs are kept 30 days.
