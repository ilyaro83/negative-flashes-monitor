#!/bin/bash
# Run this script with an AWS profile that has write access (Lambda, IAM, S3, EventBridge).
# Usage: AWS_PROFILE=<admin-profile> bash setup_lambda.sh
set -euo pipefail

ACCOUNT_ID="469351852260"
REGION="eu-west-1"
FUNCTION_NAME="negative-flashes-report"
ROLE_NAME="negative-flashes-lambda-role"
STATE_BUCKET="mylo-negative-flashes-state"
SUBNET_1="subnet-03e32a5d91f8e8697"
SUBNET_2="subnet-020081e2bed3c470b"
SG="sg-08a0540e43628e060"

# ── Fill in these values before running ──────────────────────────────────────
DB_HOST="pg-mylo-prod-reader.c9y6kmqy45u8.eu-west-1.rds.amazonaws.com"
DB_NAME="mylo"
DB_USER="postgres"
DB_PASSWORD=""          # <-- fill in
SLACK_BOT_TOKEN=""      # <-- fill in
SLACK_CHANNEL_ID="C0ADH888Z51"
# ─────────────────────────────────────────────────────────────────────────────

echo "==> 1. Creating S3 state bucket..."
aws s3api create-bucket \
  --bucket "$STATE_BUCKET" \
  --region "$REGION" \
  --create-bucket-configuration LocationConstraint="$REGION" 2>/dev/null || echo "    Bucket already exists, skipping."

aws s3api put-public-access-block \
  --bucket "$STATE_BUCKET" \
  --public-access-block-configuration "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true"

echo "==> 2. Creating IAM role..."
aws iam create-role \
  --role-name "$ROLE_NAME" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": { "Service": "lambda.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }]
  }' 2>/dev/null || echo "    Role already exists, skipping."

aws iam attach-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-arn "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"

aws iam put-role-policy \
  --role-name "$ROLE_NAME" \
  --policy-name "negative-flashes-s3-state" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": [\"s3:GetObject\", \"s3:PutObject\"],
      \"Resource\": \"arn:aws:s3:::${STATE_BUCKET}/*\"
    }]
  }"

echo "    Waiting 10s for IAM role to propagate..."
sleep 10

echo "==> 3. Building Lambda deployment package..."
rm -rf /tmp/lambda_build && mkdir /tmp/lambda_build
pip install \
  --platform manylinux2014_x86_64 \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  --target /tmp/lambda_build \
  psycopg2-binary requests -q

cp "$(dirname "$0")/lambda_function.py" /tmp/lambda_build/

cd /tmp/lambda_build
zip -r /tmp/negative_flashes_lambda.zip . -q
cd - > /dev/null
echo "    Package size: $(du -sh /tmp/negative_flashes_lambda.zip | cut -f1)"

echo "==> 4. Creating Lambda function..."
ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${ROLE_NAME}"

aws lambda create-function \
  --function-name "$FUNCTION_NAME" \
  --runtime python3.11 \
  --role "$ROLE_ARN" \
  --handler lambda_function.lambda_handler \
  --zip-file fileb:///tmp/negative_flashes_lambda.zip \
  --timeout 300 \
  --memory-size 256 \
  --region "$REGION" \
  --vpc-config "SubnetIds=${SUBNET_1},${SUBNET_2},SecurityGroupIds=${SG}" \
  --environment "Variables={
    DB_HOST=${DB_HOST},
    DB_PORT=5432,
    DB_NAME=${DB_NAME},
    DB_USER=${DB_USER},
    DB_PASSWORD=${DB_PASSWORD},
    SLACK_BOT_TOKEN=${SLACK_BOT_TOKEN},
    SLACK_CHANNEL_ID=${SLACK_CHANNEL_ID},
    STATE_BUCKET=${STATE_BUCKET}
  }" 2>/dev/null || \
aws lambda update-function-code \
  --function-name "$FUNCTION_NAME" \
  --zip-file fileb:///tmp/negative_flashes_lambda.zip \
  --region "$REGION" > /dev/null && echo "    Function already exists — code updated."

echo "==> 5. Creating EventBridge Scheduler (9 AM Israel time daily)..."
LAMBDA_ARN="arn:aws:lambda:${REGION}:${ACCOUNT_ID}:function:${FUNCTION_NAME}"

# Create scheduler execution role
aws iam create-role \
  --role-name "negative-flashes-scheduler-role" \
  --assume-role-policy-document '{
    "Version": "2012-10-17",
    "Statement": [{
      "Effect": "Allow",
      "Principal": { "Service": "scheduler.amazonaws.com" },
      "Action": "sts:AssumeRole"
    }]
  }' 2>/dev/null || echo "    Scheduler role already exists, skipping."

aws iam put-role-policy \
  --role-name "negative-flashes-scheduler-role" \
  --policy-name "invoke-lambda" \
  --policy-document "{
    \"Version\": \"2012-10-17\",
    \"Statement\": [{
      \"Effect\": \"Allow\",
      \"Action\": \"lambda:InvokeFunction\",
      \"Resource\": \"${LAMBDA_ARN}\"
    }]
  }"

SCHEDULER_ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/negative-flashes-scheduler-role"

aws scheduler create-schedule \
  --name "negative-flashes-daily" \
  --schedule-expression "cron(0 9 * * ? *)" \
  --schedule-expression-timezone "Asia/Jerusalem" \
  --flexible-time-window '{"Mode": "OFF"}' \
  --target "{
    \"Arn\": \"${LAMBDA_ARN}\",
    \"RoleArn\": \"${SCHEDULER_ROLE_ARN}\"
  }" \
  --region "$REGION" 2>/dev/null || echo "    Scheduler already exists, skipping."

echo ""
echo "✅ Done! Lambda '${FUNCTION_NAME}' will run every day at 9:00 AM Israel time."
echo "   Logs: https://${REGION}.console.aws.amazon.com/cloudwatch/home?region=${REGION}#logsV2:log-groups/log-group=/aws/lambda/${FUNCTION_NAME}"
