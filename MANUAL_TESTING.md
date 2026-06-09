# Manual Testing Plan for datasette-litestream

This document provides a comprehensive manual testing plan for the `datasette-litestream` plugin, including all AWS CLI commands needed to create the necessary resources.

> **⚠️ Litestream 0.5 migration note**
>
> As of plugin version `0.3a0`, this plugin targets **Litestream 0.5.x** and
> drives a single long-lived `litestream replicate` daemon over its control
> socket. The configuration model changed accordingly:
>
> - Database-level config uses a single `replica:` URL (not a `replicas:` list).
> - Per-database tuning keys (`monitor-interval`, `checkpoint-interval`,
>   `min/max-checkpoint-page-count`) are not yet wired through the control socket.
> - Replica backups are now laid out under `<replica>/ltx/` (LTX format), not
>   `<replica>/generations/`.
> - New scenarios to cover: registering and unregistering a database at runtime
>   via `POST /-/litestream/register` and `POST /-/litestream/unregister`
>   (requires the `litestream-manage` permission).
>
> Some scenarios below still describe the older `replicas:`/`generations/`
> behavior and should be updated when exercised against 0.5.

## Table of Contents

1. [Prerequisites](#prerequisites)
2. [AWS Resource Setup](#aws-resource-setup)
3. [Test Environment Setup](#test-environment-setup)
4. [Test Scenarios](#test-scenarios)
5. [Cleanup](#cleanup)

---

## Prerequisites

### Required Tools

```bash
echo "Installing AWS CLI (if not already installed)..."
curl "https://awscli.amazonaws.com/awscli-exe-linux-x86_64.zip" -o "awscliv2.zip"
unzip awscliv2.zip
sudo ./aws/install

echo "Verifying AWS CLI installation..."
aws --version

echo "Configure AWS CLI with your credentials..."
aws configure
echo "Enter: AWS Access Key ID, Secret Access Key, Region (e.g., us-east-1), Output format (json)"

echo "Installing uv (Python package manager)..."
curl -LsSf https://astral.sh/uv/install.sh | sh

echo "Cloning the repository..."
git clone https://github.com/datasette/datasette-litestream.git ~/dev/ecosystem/datasette-litestream
cd ~/dev/ecosystem/datasette-litestream
git checkout restarts
```

### Required Python Environment

```bash
cd ~/dev/ecosystem/datasette-litestream

echo "Installing dependencies using uv..."
uv sync

echo "Verifying the plugin loads..."
uv run datasette --version
uv run datasette plugins | grep litestream
```

---

## AWS Resource Setup

### Variables (customize these)

```bash
echo "Setting variables for all subsequent commands..."
export AWS_REGION="us-east-1"
export BUCKET_NAME="datasette-litestream-test-$(date +%s)"
export IAM_USER_NAME="datasette-litestream-test-user"
export IAM_ROLE_NAME="datasette-litestream-test-role"
export IAM_POLICY_NAME="datasette-litestream-s3-policy"
export ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
```

### 1. Create S3 Bucket

```bash
echo "Creating the S3 bucket..."
aws s3api create-bucket \
    --bucket "$BUCKET_NAME" \
    --region "$AWS_REGION"

echo "Verifying bucket creation..."
aws s3api head-bucket --bucket "$BUCKET_NAME"
echo "Bucket created: $BUCKET_NAME"
```

### 2. Create IAM Policy for S3 Access

```bash
echo "Creating the IAM policy document..."
cat > ./litestream-s3-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Sid": "LitestreamS3Access",
            "Effect": "Allow",
            "Action": [
                "s3:GetObject",
                "s3:PutObject",
                "s3:DeleteObject",
                "s3:ListBucket",
                "s3:GetBucketLocation"
            ],
            "Resource": [
                "arn:aws:s3:::${BUCKET_NAME}",
                "arn:aws:s3:::${BUCKET_NAME}/*"
            ]
        }
    ]
}
EOF

echo "Creating the IAM policy..."
export POLICY_ARN=$(aws iam create-policy \
    --policy-name "$IAM_POLICY_NAME" \
    --policy-document file://./litestream-s3-policy.json \
    --query 'Policy.Arn' \
    --output text)

echo "Policy ARN: $POLICY_ARN"
```

### 3. Create IAM User with Static Credentials

```bash
echo "Creating IAM user..."
aws iam create-user --user-name "$IAM_USER_NAME"

echo "Attaching the S3 policy to the user..."
aws iam attach-user-policy \
    --user-name "$IAM_USER_NAME" \
    --policy-arn "$POLICY_ARN"

echo "Creating access keys for the user..."
aws iam create-access-key --user-name "$IAM_USER_NAME" > ./access-keys.json

echo "Extracting the credentials..."
export LITESTREAM_ACCESS_KEY_ID=$(jq -r '.AccessKey.AccessKeyId' ./access-keys.json)
export LITESTREAM_SECRET_ACCESS_KEY=$(jq -r '.AccessKey.SecretAccessKey' ./access-keys.json)

echo "Access Key ID: $LITESTREAM_ACCESS_KEY_ID"
echo "Secret Access Key: $LITESTREAM_SECRET_ACCESS_KEY"

echo "Saving credentials to a file for later use..."
cat > ./static-credentials.json << EOF
{
    "access-key-id": "$LITESTREAM_ACCESS_KEY_ID",
    "secret-access-key": "$LITESTREAM_SECRET_ACCESS_KEY"
}
EOF

echo "Credentials saved to ./static-credentials.json"
```

### 4. Create IAM Role for STS Temporary Credentials

```bash
echo "Creating trust policy for the role (allows the IAM user to assume it)..."
cat > ./trust-policy.json << EOF
{
    "Version": "2012-10-17",
    "Statement": [
        {
            "Effect": "Allow",
            "Principal": {
                "AWS": "arn:aws:iam::${ACCOUNT_ID}:user/${IAM_USER_NAME}"
            },
            "Action": "sts:AssumeRole"
        }
    ]
}
EOF

echo "Creating the IAM role..."
aws iam create-role \
    --role-name "$IAM_ROLE_NAME" \
    --assume-role-policy-document file://./trust-policy.json

echo "Attaching the S3 policy to the role..."
aws iam attach-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-arn "$POLICY_ARN"

echo "Getting the role ARN for later use..."
export ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IAM_ROLE_NAME}"
echo "Role ARN: $ROLE_ARN"
```

### 5. Create Script for Fetching Temporary Credentials

```bash
echo "Creating a script that fetches temporary STS credentials..."
cat > ./fetch-sts-credentials.sh << 'SCRIPT'
#!/bin/bash

ROLE_ARN="$1"

if [ -z "$ROLE_ARN" ]; then
    echo "Usage: $0 <role-arn>" >&2
    exit 1
fi

CREDS=$(aws sts assume-role \
    --role-arn "$ROLE_ARN" \
    --role-session-name "datasette-litestream-session" \
    --duration-seconds 3600 \
    --output json)

if [ $? -ne 0 ]; then
    echo "Failed to assume role" >&2
    exit 1
fi

echo "$CREDS" | jq '{
    "access-key-id": .Credentials.AccessKeyId,
    "secret-access-key": .Credentials.SecretAccessKey,
    "session-token": .Credentials.SessionToken
}'
SCRIPT

chmod +x ./fetch-sts-credentials.sh

echo "STS credential fetch script created at ./fetch-sts-credentials.sh"
```

---

## Test Environment Setup

### Create Test Database

```bash
echo "Creating a test SQLite database..."
(cd ~/dev/ecosystem/datasette-litestream && uv run python << 'EOF'
import sqlite3
import os

db_path = "./test-database.db"

if os.path.exists(db_path):
    os.remove(db_path)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE users (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        email TEXT UNIQUE,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

cursor.execute('''
    CREATE TABLE orders (
        id INTEGER PRIMARY KEY,
        user_id INTEGER,
        product TEXT,
        amount REAL,
        order_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (user_id) REFERENCES users(id)
    )
''')

cursor.executemany('INSERT INTO users (name, email) VALUES (?, ?)', [
    ('Alice', 'alice@example.com'),
    ('Bob', 'bob@example.com'),
    ('Charlie', 'charlie@example.com'),
])

cursor.executemany('INSERT INTO orders (user_id, product, amount) VALUES (?, ?, ?)', [
    (1, 'Widget A', 29.99),
    (1, 'Widget B', 49.99),
    (2, 'Gadget X', 99.99),
])

conn.commit()
conn.close()
print(f"Test database created at {db_path}")
EOF
)

echo "Creating a second test database for multi-database tests..."
(cd ~/dev/ecosystem/datasette-litestream && uv run python << 'EOF'
import sqlite3
import os

db_path = "./analytics.db"

if os.path.exists(db_path):
    os.remove(db_path)

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

cursor.execute('''
    CREATE TABLE events (
        id INTEGER PRIMARY KEY,
        event_type TEXT,
        payload TEXT,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
''')

cursor.executemany('INSERT INTO events (event_type, payload) VALUES (?, ?)', [
    ('page_view', '{"page": "/home"}'),
    ('click', '{"button": "signup"}'),
    ('page_view', '{"page": "/pricing"}'),
])

conn.commit()
conn.close()
print(f"Analytics database created at {db_path}")
EOF
)
```

---

## Test Scenarios

### Test 1: Basic Replication with Environment Variables

**Purpose:** Verify basic S3 replication works with credentials from environment variables.

```bash
echo "Ensuring credentials are exported (from setup step)..."
export LITESTREAM_ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
export LITESTREAM_SECRET_ACCESS_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)

echo "Starting Datasette with environment variable credentials..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Open http://localhost:8001/-/litestream-status in a browser"
echo "2. Sign in as root (click the link shown in terminal)"
echo "3. Verify 'Litestream status' page shows:"
echo "   - Process status: alive"
echo "   - Metrics section is visible"
echo "   - No error logs"
echo "4. Wait 5-10 seconds for initial replication"
echo "5. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.metrics-addr ":9091" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test1/$DB_NAME"]' \
    -p 8001 \
    --root)

echo ""
echo "Verifying data was replicated to S3..."
aws s3 ls "s3://${BUCKET_NAME}/test1/test-database/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Files should appear in S3 bucket under test1/test-database/"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 2: Replication with Static Credentials via CLI Args

**Purpose:** Verify credentials can be specified directly via CLI arguments.

```bash
echo "Unsetting environment variables to ensure config is used..."
unset LITESTREAM_ACCESS_KEY_ID
unset LITESTREAM_SECRET_ACCESS_KEY

echo "Reading credentials..."
export ACCESS_KEY=$(jq -r '."access-key-id"' ./static-credentials.json)
export SECRET_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)

echo "Starting Datasette with CLI-based credentials..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Access http://localhost:8002/-/litestream-status"
echo "2. Verify replication is working"
echo "3. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.access-key-id "${ACCESS_KEY}" \
    -s plugins.datasette-litestream.secret-access-key "${SECRET_KEY}" \
    -s plugins.datasette-litestream.metrics-addr ":9092" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test2/$DB_NAME"]' \
    -p 8002 \
    --root)

echo ""
echo "Verifying data was replicated to S3..."
aws s3 ls "s3://${BUCKET_NAME}/test2/test-database/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Replication works with credentials from CLI args"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 3: Replication with Dynamic Credentials from File

**Purpose:** Test the `credentials-file` option for loading credentials from a JSON file.

```bash
echo "Ensuring no env vars interfere..."
unset LITESTREAM_ACCESS_KEY_ID
unset LITESTREAM_SECRET_ACCESS_KEY

echo "Credentials file already exists at ./static-credentials.json"
export CREDENTIALS_FILE_PATH="$(pwd)/static-credentials.json"

echo "Starting Datasette with file-based dynamic credentials..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Access http://localhost:8003/-/litestream-status"
echo "2. Verify litestream is running"
echo "3. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-file "${CREDENTIALS_FILE_PATH}" \
    -s plugins.datasette-litestream.credentials-refresh-interval 60 \
    -s plugins.datasette-litestream.metrics-addr ":9093" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test3/$DB_NAME"]' \
    -p 8003 \
    --root)

echo ""
echo "Verifying data was replicated to S3..."
aws s3 ls "s3://${BUCKET_NAME}/test3/test-database/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Replication works with credentials loaded from file"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 4: Replication with Dynamic Credentials from Command

**Purpose:** Test the `credentials-command` option for fetching credentials via a script.

```bash
echo "Unsetting environment variables..."
unset LITESTREAM_ACCESS_KEY_ID
unset LITESTREAM_SECRET_ACCESS_KEY

echo "Creating a simple credential command script..."
export CREDENTIALS_FILE_PATH="$(pwd)/static-credentials.json"
cat > ./get-credentials.sh << EOF
#!/bin/bash
cat ${CREDENTIALS_FILE_PATH}
EOF
chmod +x ./get-credentials.sh

export CREDENTIALS_COMMAND="$(pwd)/get-credentials.sh"

echo "Starting Datasette with command-based dynamic credentials..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Access http://localhost:8004/-/litestream-status"
echo "2. Verify litestream is running"
echo "3. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-command "${CREDENTIALS_COMMAND}" \
    -s plugins.datasette-litestream.credentials-refresh-interval 60 \
    -s plugins.datasette-litestream.metrics-addr ":9094" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test4/$DB_NAME"]' \
    -p 8004 \
    --root)

echo ""
echo "Verifying data was replicated to S3..."
aws s3 ls "s3://${BUCKET_NAME}/test4/test-database/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Replication works with credentials from command output"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 5: Replication with AWS STS Temporary Credentials

**Purpose:** Test replication using temporary AWS STS credentials with session tokens.

```bash
echo "Setting up credentials for the IAM user (to assume the role)..."
export AWS_ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
export AWS_SECRET_ACCESS_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)
unset LITESTREAM_ACCESS_KEY_ID
unset LITESTREAM_SECRET_ACCESS_KEY

echo "Testing role assumption..."
export ROLE_ARN="arn:aws:iam::${ACCOUNT_ID}:role/${IAM_ROLE_NAME}"
./fetch-sts-credentials.sh "$ROLE_ARN"

export FETCH_CREDS_COMMAND="$(pwd)/fetch-sts-credentials.sh ${ROLE_ARN}"

echo ""
echo "Starting Datasette with STS temporary credentials..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Access http://localhost:8005/-/litestream-status"
echo "2. Verify litestream is running (session-token is being used)"
echo "3. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-command "${FETCH_CREDS_COMMAND}" \
    -s plugins.datasette-litestream.credentials-refresh-interval 300 \
    -s plugins.datasette-litestream.metrics-addr ":9095" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test5/$DB_NAME"]' \
    -p 8005 \
    --root)

echo ""
echo "Verifying data was replicated to S3..."
aws s3 ls "s3://${BUCKET_NAME}/test5/test-database/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Replication works with STS temporary credentials including session token"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 6: Credential Rotation / Refresh

**Purpose:** Verify that credentials are automatically refreshed and litestream restarts when credentials change.

```bash
echo "Creating initial credentials file..."
cat > ./rotating-credentials.json << EOF
{
    "access-key-id": "$(jq -r '."access-key-id"' ./static-credentials.json)",
    "secret-access-key": "$(jq -r '."secret-access-key"' ./static-credentials.json)"
}
EOF

export ROTATING_CREDENTIALS_PATH="$(pwd)/rotating-credentials.json"

echo "Starting Datasette with 10-second credential refresh..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Start datasette (it will run in foreground)"
echo "2. Open a NEW TERMINAL and modify the credentials file:"
echo "   echo '{\"access-key-id\": \"CHANGED\", \"secret-access-key\": \"CHANGED\"}' > ${ROTATING_CREDENTIALS_PATH}"
echo "3. Watch THIS terminal for the message:"
echo "   'datasette-litestream: credentials changed, restarting litestream'"
echo "4. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-file "${ROTATING_CREDENTIALS_PATH}" \
    -s plugins.datasette-litestream.credentials-refresh-interval 10 \
    -s plugins.datasette-litestream.metrics-addr ":9096" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test6/$DB_NAME"]' \
    -p 8006 \
    --root)

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Litestream automatically restarts when credentials change"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 7: Multiple Database Replication

**Purpose:** Test that all-replicate works correctly with multiple databases.

```bash
export LITESTREAM_ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
export LITESTREAM_SECRET_ACCESS_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)

echo "Starting Datasette with multiple databases..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Access http://localhost:8007/-/litestream-status"
echo "2. Verify both databases are shown in the config"
echo "3. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db ./analytics.db \
    -s plugins.datasette-litestream.metrics-addr ":9097" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test7/$DB_NAME"]' \
    -p 8007 \
    --root)

echo ""
echo "Verifying both databases were replicated to S3..."
echo "Checking test-database:"
aws s3 ls "s3://${BUCKET_NAME}/test7/test-database/" --recursive

echo ""
echo "Checking analytics:"
aws s3 ls "s3://${BUCKET_NAME}/test7/analytics/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Both databases are replicated with \$DB_NAME variable correctly expanded"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 8: Database-Level Configuration

**Purpose:** Test per-database replica configuration.

```bash
export LITESTREAM_ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
export LITESTREAM_SECRET_ACCESS_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)

echo "Starting Datasette with per-database configuration..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Access http://localhost:8008/-/litestream-status"
echo "2. Check the litestream config shown on the page has both databases"
echo "3. Press Ctrl+C to stop when done"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db ./analytics.db \
    -s plugins.datasette-litestream.metrics-addr ":9098" \
    -s databases.test-database.plugins.datasette-litestream.replicas '[{"url": "s3://'"${BUCKET_NAME}"'/test8/primary-db"}]' \
    -s databases.test-database.plugins.datasette-litestream.monitor-interval "1s" \
    -s databases.test-database.plugins.datasette-litestream.checkpoint-interval "30s" \
    -s databases.analytics.plugins.datasette-litestream.replicas '[{"url": "s3://'"${BUCKET_NAME}"'/test8/analytics-db"}]' \
    -s databases.analytics.plugins.datasette-litestream.monitor-interval "5s" \
    -p 8008 \
    --root)

echo ""
echo "Verifying database-specific replication to S3..."
echo "Checking primary-db:"
aws s3 ls "s3://${BUCKET_NAME}/test8/primary-db/" --recursive

echo ""
echo "Checking analytics-db:"
aws s3 ls "s3://${BUCKET_NAME}/test8/analytics-db/" --recursive

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Each database replicates to its own configured path"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 9: Prometheus Metrics

**Purpose:** Verify the Prometheus metrics endpoint works correctly.

```bash
export LITESTREAM_ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
export LITESTREAM_SECRET_ACCESS_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)

echo "Starting Datasette with metrics enabled..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Datasette will start in the background"
echo "2. We will fetch Prometheus metrics automatically"
echo "3. Look for metrics like:"
echo "   - litestream_replica_operation_total"
echo "   - litestream_replica_operation_bytes_total"
echo "   - litestream_db_*"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.metrics-addr ":9099" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test9/$DB_NAME"]' \
    -p 8009 \
    --root) &

export DATASETTE_PID=$!
sleep 5

echo ""
echo "Fetching Prometheus metrics from litestream..."
echo "=========================================="
curl -s http://localhost:9099/metrics | head -50
echo "=========================================="

kill $DATASETTE_PID 2>/dev/null

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Prometheus metrics are exposed and include litestream-specific metrics"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 10: Data Integrity Test

**Purpose:** Verify that data written to SQLite is properly replicated and can be restored.

```bash
export LITESTREAM_ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
export LITESTREAM_SECRET_ACCESS_KEY=$(jq -r '."secret-access-key"' ./static-credentials.json)

echo "Creating a fresh database for this test..."
rm -f ./integrity-test.db

(cd ~/dev/ecosystem/datasette-litestream && uv run python << 'EOF'
import sqlite3
conn = sqlite3.connect('./integrity-test.db')
conn.execute('CREATE TABLE data (id INTEGER PRIMARY KEY, value TEXT)')
conn.execute("INSERT INTO data (value) VALUES ('initial')")
conn.commit()
conn.close()
print("Initial database created")
EOF
)

echo "Starting Datasette for integrity test..."
echo ""
echo "=========================================="
echo "TEST WILL RUN AUTOMATICALLY:"
echo "1. Start datasette and wait for initial replication"
echo "2. Add more data to the database"
echo "3. Stop datasette"
echo "4. Restore from S3 backup"
echo "5. Verify data integrity"
echo "=========================================="
echo ""
read "?Press Enter to start the integrity test..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./integrity-test.db \
    -s plugins.datasette-litestream.metrics-addr ":9100" \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test10/$DB_NAME"]' \
    -p 8010 \
    --root) &

export DATASETTE_PID=$!
echo "Waiting for initial replication..."
sleep 5

echo "Adding more data..."
(cd ~/dev/ecosystem/datasette-litestream && uv run python << 'EOF'
import sqlite3
conn = sqlite3.connect('./integrity-test.db')
for i in range(1, 11):
    conn.execute(f"INSERT INTO data (value) VALUES ('value_{i}')")
conn.commit()
conn.close()
print("Added 10 more rows")
EOF
)

echo "Waiting for replication..."
sleep 5

echo "Stopping datasette..."
kill $DATASETTE_PID 2>/dev/null
sleep 2

echo "Testing restoration..."

rm -f ./restored.db

echo "Getting litestream binary path..."
export LITESTREAM_BIN=$(cd ~/dev/ecosystem/datasette-litestream && uv run python -c "from datasette_litestream import resolve_litestream_path; print(resolve_litestream_path())")

echo "Using litestream binary at: $LITESTREAM_BIN"

echo "Creating a restore config..."
cat > ./restore-config.yaml << EOF
access-key-id: ${LITESTREAM_ACCESS_KEY_ID}
secret-access-key: ${LITESTREAM_SECRET_ACCESS_KEY}
dbs:
  - path: ./restored.db
    replicas:
      - url: s3://${BUCKET_NAME}/test10/integrity-test
EOF

echo "Restoring..."
$LITESTREAM_BIN restore -config ./restore-config.yaml ./restored.db

echo "Verifying restored data..."
(cd ~/dev/ecosystem/datasette-litestream && uv run python << 'EOF'
import sqlite3
conn = sqlite3.connect('./restored.db')
cursor = conn.execute('SELECT COUNT(*) FROM data')
count = cursor.fetchone()[0]
print(f"Restored database has {count} rows")
if count >= 11:
    print("✓ Data integrity verified!")
else:
    print("✗ Data may be missing")
conn.close()
EOF
)

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Restored database contains all data that was written (11+ rows)"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 11: Error Handling - Invalid Credentials

**Purpose:** Verify proper error handling when credentials are invalid.

```bash
unset LITESTREAM_ACCESS_KEY_ID
unset LITESTREAM_SECRET_ACCESS_KEY

echo "Creating invalid credentials file..."
cat > ./invalid-credentials.json << EOF
{
    "access-key-id": "INVALID_KEY",
    "secret-access-key": "INVALID_SECRET"
}
EOF

export INVALID_CREDENTIALS_PATH="$(pwd)/invalid-credentials.json"

echo "Starting Datasette with invalid credentials..."
echo ""
echo "=========================================="
echo "EXPECTED BEHAVIOR:"
echo "Litestream should fail to authenticate and show error messages"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette (will timeout after 10 seconds)..."

timeout 10 bash -c '(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-file "'"${INVALID_CREDENTIALS_PATH}"'" \
    -s plugins.datasette-litestream.credentials-refresh-interval 60 \
    -s plugins.datasette-litestream.all-replicate '"'"'["s3://'"${BUCKET_NAME}"'/test11/$DB_NAME"]'"'"' \
    -p 8011 \
    --root 2>&1)' || echo "Process exited (expected)"

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Clear error message about authentication failure"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 12: Error Handling - Missing Credentials File

**Purpose:** Verify proper error handling when credentials file doesn't exist.

```bash
echo "Starting Datasette with missing credentials file..."
echo ""
echo "=========================================="
echo "EXPECTED BEHAVIOR:"
echo "StartupError about failed to load credentials"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-file "/nonexistent/credentials.json" \
    -s plugins.datasette-litestream.credentials-refresh-interval 60 \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test12/$DB_NAME"]' \
    -p 8012 \
    --root 2>&1) || echo "Startup failed as expected"

echo ""
echo "=========================================="
echo "EXPECTED RESULT: StartupError: datasette-litestream: failed to load initial credentials"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 13: Error Handling - Both credentials-file and credentials-command

**Purpose:** Verify error when both credential sources are specified.

```bash
export CREDENTIALS_FILE_PATH="$(pwd)/static-credentials.json"

echo "Starting Datasette with both credential sources..."
echo ""
echo "=========================================="
echo "EXPECTED BEHAVIOR:"
echo "StartupError about cannot specify both"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.credentials-file "${CREDENTIALS_FILE_PATH}" \
    -s plugins.datasette-litestream.credentials-command "echo {}" \
    -s plugins.datasette-litestream.credentials-refresh-interval 60 \
    -s plugins.datasette-litestream.all-replicate '["s3://'"${BUCKET_NAME}"'/test13/$DB_NAME"]' \
    -p 8013 \
    --root 2>&1) || echo "Startup failed as expected"

echo ""
echo "=========================================="
echo "EXPECTED RESULT: StartupError: datasette-litestream: cannot specify both 'credentials-file' and 'credentials-command'"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to the next test..."
```

---

### Test 14: Local File Backup (No AWS Required)

**Purpose:** Test replication to local filesystem (useful for development/testing without AWS).

```bash
echo "No AWS credentials needed for file:// replicas"
unset LITESTREAM_ACCESS_KEY_ID
unset LITESTREAM_SECRET_ACCESS_KEY
unset AWS_ACCESS_KEY_ID
unset AWS_SECRET_ACCESS_KEY

rm -rf ./local-backup

echo "Starting Datasette with local file backup..."
echo ""
echo "=========================================="
echo "VERIFICATION STEPS:"
echo "1. Datasette will start in the background"
echo "2. We will check the local backup directory"
echo "=========================================="
echo ""
read "?Press Enter to start Datasette..."

export LOCAL_BACKUP_PATH="$(pwd)/local-backup"

(cd ~/dev/ecosystem/datasette-litestream && uv run datasette ./test-database.db \
    -s plugins.datasette-litestream.metrics-addr ":9114" \
    -s plugins.datasette-litestream.all-replicate '["file://'"${LOCAL_BACKUP_PATH}"'/$DB_NAME"]' \
    -p 8014 \
    --root) &

export DATASETTE_PID=$!
sleep 3

echo "Checking local backup directory..."
ls -la ./local-backup/ 2>/dev/null || echo "local-backup directory not found yet"
echo ""
ls -la ./local-backup/test-database/ 2>/dev/null || echo "No test-database backup yet"

sleep 3
echo ""
echo "After waiting:"
ls -la ./local-backup/test-database/ 2>/dev/null || echo "Still no backup"

kill $DATASETTE_PID 2>/dev/null

echo ""
echo "=========================================="
echo "EXPECTED RESULT: Backup files created at ./local-backup/test-database/"
echo "=========================================="
read "?Did the test pass? Press Enter to continue to cleanup..."
```

---

## Cleanup

### Clean Up AWS Resources

```bash
echo "Starting AWS cleanup..."
echo ""
read "?Press Enter to clean up AWS resources..."

echo "Deleting S3 bucket contents..."
aws s3 rm "s3://${BUCKET_NAME}" --recursive

echo "Deleting S3 bucket..."
aws s3api delete-bucket --bucket "$BUCKET_NAME"

echo "Cleaning up IAM user..."
aws iam detach-user-policy \
    --user-name "$IAM_USER_NAME" \
    --policy-arn "$POLICY_ARN"

echo "Deleting access keys..."
export ACCESS_KEY_ID=$(jq -r '."access-key-id"' ./static-credentials.json)
aws iam delete-access-key \
    --user-name "$IAM_USER_NAME" \
    --access-key-id "$ACCESS_KEY_ID"

aws iam delete-user --user-name "$IAM_USER_NAME"

echo "Cleaning up IAM role..."
aws iam detach-role-policy \
    --role-name "$IAM_ROLE_NAME" \
    --policy-arn "$POLICY_ARN"

aws iam delete-role --role-name "$IAM_ROLE_NAME"

echo "Deleting IAM policy..."
aws iam delete-policy --policy-arn "$POLICY_ARN"

echo "AWS cleanup complete!"
```

### Clean Up Local Files

```bash
echo "Starting local cleanup..."
echo ""
read "?Press Enter to clean up local files..."

echo "Removing test files..."
rm -f ./test-database.db
rm -f ./analytics.db
rm -f ./integrity-test.db
rm -f ./restored.db
rm -rf ./local-backup
rm -f ./litestream-s3-policy.json
rm -f ./trust-policy.json
rm -f ./access-keys.json
rm -f ./static-credentials.json
rm -f ./rotating-credentials.json
rm -f ./invalid-credentials.json
rm -f ./get-credentials.sh
rm -f ./fetch-sts-credentials.sh
rm -f ./restore-config.yaml

echo "Local cleanup complete!"
```

---

## Test Summary Checklist

| Test | Description | Pass/Fail |
|------|-------------|-----------|
| 1 | Basic replication with env vars | x |
| 2 | Static credentials via CLI args | x |
| 3 | Dynamic credentials from file | x |
| 4 | Dynamic credentials from command | x |
| 5 | STS temporary credentials | x |
| 6 | Credential rotation/refresh | ☐ |
| 7 | Multiple database replication | x |
| 8 | Database-level configuration | x |
| 9 | Prometheus metrics | x |
| 10 | Data integrity (backup/restore) | ☐ |
| 11 | Error: Invalid credentials | x |
| 12 | Error: Missing credentials file | x |
| 13 | Error: Both credential sources | x |
| 14 | Local file backup (no AWS) | x |

---

## Notes

### IAM Permissions Required for Testing

The test user/role needs these S3 permissions:
- `s3:GetObject` - Read replica data
- `s3:PutObject` - Write replica data
- `s3:DeleteObject` - Clean up old generations
- `s3:ListBucket` - List bucket contents
- `s3:GetBucketLocation` - Determine bucket region

### Troubleshooting

**Litestream not found:**
```bash
echo "Checking if litestream is bundled with the package..."
ls -la ~/dev/ecosystem/datasette-litestream/datasette_litestream/bin/

echo "Or install system-wide..."
curl -L https://github.com/benbjohnson/litestream/releases/download/v0.3.13/litestream-v0.3.13-linux-amd64.tar.gz | tar xz
sudo mv litestream /usr/local/bin/
```

**S3 Access Denied:**
```bash
echo "Verifying credentials work..."
aws s3 ls "s3://${BUCKET_NAME}/"

echo "Checking IAM policy is attached..."
aws iam list-attached-user-policies --user-name "$IAM_USER_NAME"
```

**Plugin not loading:**
```bash
echo "Verifying installation..."
(cd ~/dev/ecosystem/datasette-litestream && uv run datasette plugins)

echo "Checking for errors..."
(cd ~/dev/ecosystem/datasette-litestream && uv run python -c "import datasette_litestream; print('OK')")
```
