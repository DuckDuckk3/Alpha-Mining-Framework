# Alpha Mining Framework

An LLM-based intelligent Alpha factor generation system that automatically generates, tests, and submits Alpha factors to the WorldQuant Brain platform.

## Core Features

* **Dual LLM Support** — Supports both the DeepSeek API and local Ollama models
* **Dynamic Track Weighting** — Reinforcement-learning-style module selection that dynamically adjusts weights according to success rates
* **Intelligent Field Sampling** — Uses a Log+MinMax+Softmax algorithm to balance field selection and prevent price-volume data from dominating
* **Nonlinear Genetic Recombination** — Extracts elite factors from the shared pool and uses nonlinear operators such as `ts_corr`, `ts_cov`, and `rank` for crossover
* **Parameter Optimization** — Automatically attempts representative parameter combinations when checks fail
* **Intelligent Rescue Pool** — Automatically places borderline factors into a rescue pool and performs targeted repairs for failed checks
* **Reverse Factor Detection** — Automatically negates factors with negative Sharpe and retests them
* **Shared Factor Pool** — Supports team collaboration through a distributed factor pool
* **Feishu Notifications** — Automatically pushes notifications for Alpha discoveries, periodic summaries, correlation checks, and emergency circuit breakers
* **Expression Validation** — Automatically validates whether variables in expressions belong to the available field set
* **Strict Filtering** — Only saves factors that meet the required performance and validation conditions
* **Manual Submission** — Factors are not submitted automatically; submission is controlled manually through a separate script

## System Requirements

* Python ≥ 3.11
* WorldQuant Brain account
* DeepSeek API Key or local Ollama service

## Quick Start

### 1. Install Dependencies

```bash
pip install requests pandas python-dotenv openai

# Or use uv
uv sync
```

### 2. Configure Environment Variables

```bash
cp .env.example .env
```

Edit `.env`:

```text
# WorldQuant Brain credentials
WQ_USERNAME=your_email@example.com
WQ_PASSWORD=your_password

# LLM configuration
# Option 1: DeepSeek API
DEEPSEEK_API_KEY=your_deepseek_api_key

# Option 2: Ollama local model
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=qwen3:8b
```

### 3. Fetch Data Fields and Operators

Fetch all available data fields and operators from the WorldQuant Brain API:

```bash
python fetch_fields.py
```

Data fields will be saved to the `data/fields/` directory, while operators will be saved to `data/operators/operators.csv`.

### 4. Start Mining

Use DeepSeek API:

```bash
python run_alpha_miner.py
```

Specify the LLM provider:

```bash
python run_alpha_miner.py --llm deepseek
python run_alpha_miner.py --llm ollama
```

Specify a member ID for team collaboration:

```bash
python run_alpha_miner.py --member-id gao
```

Adjust concurrency:

```bash
python run_alpha_miner.py --workers 3
```

### 5. Submit Factors

Generated factors are automatically saved to the database with status `pending`.

After passing the correlation check, they become `unsubmitted` and can be submitted using:

```bash
# Submit a single factor
python submit_alpha.py <alpha_id>

# Submit multiple factors
python submit_alpha.py pwnbR9Gq akNmojM1
```

All conditions are checked during submission. If a factor fails, the detailed reason is displayed and the factor is deleted from the database.

### 6. Manually Add Factors

Add known factors from WorldQuant Brain to the database:

```bash
python add_alpha.py <alpha_id>

python add_alpha.py pwnbR9Gq akNmojM1
```

## Project Structure

```text
WorldQuant/
├── core/                          # Core business logic
│   ├── config.py                  # Environment variable and credential loading
│   ├── api_session.py             # API session management
│   ├── alpha_db.py                # SQLite Alpha database
│   ├── llm_client.py              # Unified LLM client
│   ├── submission_quota.py        # Daily submission quota tracking
│   ├── notifier.py                # Feishu notification module
│   ├── feishu_client.py           # Feishu API client
│   ├── data_fetcher.py            # Data field and operator retrieval
│   └── log_manager.py             # Log management
│
├── data/                          # Runtime data
│   ├── fields/                    # Data field CSV files
│   │   ├── analyst4.csv
│   │   ├── fundamental6.csv
│   │   ├── model77.csv
│   │   ├── pv13.csv
│   │   └── ...
│   │
│   ├── operators/
│   │   └── operators.csv
│   │
│   └── shared_pool/               # Shared factor pool
│
├── log/                           # Log files
├── run_alpha_miner.py             # Main mining program
├── submit_alpha.py                # Factor submission script
├── add_alpha.py                   # Manual factor addition script
├── check_correlation.py           # Correlation checking script
├── feishu_bot.py                  # Feishu bot command control
├── setup_cron.sh                  # Scheduled task configuration
├── fetch_fields.py                # Data retrieval script
├── mine.sh                        # One-click startup script
├── .env.example                   # Environment variable example
└── README.md                      # Project documentation
```

## Core Workflow

### 1. Field Loading

When the system starts, it loads the available datasets from the `data/fields/` directory into memory as a field pool.

The datasets include:

* `pv1.csv` — Price and volume
* `fundamental6.csv` — Fundamental data
* `analyst4.csv` — Analyst estimates
* `model77.csv` — Factor models
* `news12.csv` — News data
* And other datasets

### 2. Dynamic Module Selection and Intelligent Field Sampling

The system uses a **Log+MinMax+Softmax** algorithm to balance field selection.

#### Algorithm Workflow

1. `Log(1+x)` — Compresses extreme values to prevent large differences in Alpha counts
2. `MinMax normalization` — Maps values to the [0, 1] range
3. `Softmax(T=0.12)` — Generates a probability distribution according to the temperature parameter

#### Selection Process

1. Use Log+MinMax+Softmax to calculate dataset weights and randomly select 1–2 datasets
2. Within the selected datasets, use the same algorithm to calculate field weights and sample 15 fields
3. Use different field combinations for each generation to maximize exploration coverage

### 3. Expression Rules

#### Event Field Restrictions

Event fields such as `nws_*`, `snt_*`, `scl_*_buzz*`, and `rp_*` are VECTOR types and have strict restrictions.

* Cannot be compared using `>`, `<`, `>=`, or `<=`
* Cannot participate in arithmetic operations such as `+`, `-`, `*`, `/`
* Can be evaluated using `==` or `!=`
* Can be converted using `sign()` and then compared
* Can be directly used as the condition of `trade_when`

Example:

```text
if_else(field == 1, x, y)
```

or:

```text
if_else(sign(field) == 1, x, y)
```

#### Logical Operators

Function form must be used:

```text
and(input1, input2)
or(input1, input2)
not(input)
```

Correct:

```text
if_else(and(x > 0, y > 0), a, b)
```

Incorrect:

```text
if_else(x > 0 and y > 0, a, b)
```

Incorrect:

```text
if_else(x > 0 & y > 0, a, b)
```

### 4. Alpha Generation Strategies

#### New Mining — 60%

* Select modules according to dynamic weights
* Generate factors from fields in the selected modules

#### Nonlinear Genetic Recombination — 20%

* Randomly select two elite factors from the shared pool
* Extract their core logic
* Perform crossover using nonlinear operators such as:

  * `ts_corr(A, B, d)` — Time-series correlation
  * `ts_cov(A, B, d)` — Time-series covariance
  * `rank(A) / rank(B)` — Rank ratio
  * `sign(A) * abs(B)` — Sign combination
* Reapply the outer structure `ts_decay_linear(zscore(...), 5)`
* Generate new variants
* Neutralization is controlled by settings

#### Rescue Pool — 20%

Extract borderline factors from the rescue pool and generate repair variants targeting failed checks.

Examples:

* Turnover too high → Increase the window parameter
* Self-Correlation failure → Change the neutralization method
* Drawdown too large → Increase decay smoothing

A maximum of three rescue attempts is performed.

### 5. Result Processing

| **Condition**                                         | **Action**                                        |
| ----------------------------------------------------- | ------------------------------------------------- |
| Sharpe ≥ 1.25 and Fitness ≥ 1.0 and all checks passed | Save to database (`pending`)                      |
| Sharpe ≥ 1.25 and Fitness ≥ 1.0 but checks failed     | Parameter optimization → Decide whether to rescue |
| Sharpe > 1.0 and Fitness > 0.8                        | Add to shared pool                                |
| Sharpe < -0.8                                         | Negate the factor and retest                      |
| abs(Sharpe) + abs(Fitness) > 1.7                      | Enter rescue pool                                 |
| Other                                                 | Record failure and update module weights          |

#### Parameter Optimization Process

1. When checks fail, try four representative parameter combinations
2. Parameter combinations include different neutralization methods
3. Save to the database if any combination passes
4. If all fail:

   * `TURNOVER` / `DRAWDOWN` → Enter rescue pool
   * `SELF_CORRELATION` → Discard

### 6. Database Status

Factor status values:

* `pending` — Newly added, waiting for correlation check
* `unsubmitted` — Passed correlation check and can be submitted
* `submitted` — Successfully submitted to the platform

Final validation is performed during submission:

* Check `SELF_CORRELATION`
* Check all other checks
* Display detailed failure reasons
* Delete factors that fail validation

### 7. Shared Factor Pool

Team collaboration features:

* Each member has an independent JSON file: `shared_pool_{member_id}.json`
* All member files are merged when reading
* The top 500 factors are retained according to Sharpe
* Elite factors are selected from the shared pool during genetic recombination

## Command-Line Arguments

### `run_alpha_miner.py`

| **Parameter** | **Default** | **Description**                            |
| ------------- | ----------- | ------------------------------------------ |
| `--llm`       | `auto`      | LLM provider: `auto`, `deepseek`, `ollama` |
| `--workers`   | `2`         | Number of concurrent simulation workers    |
| `--member-id` | `default`   | Member ID for team collaboration           |

### `submit_alpha.py`

```bash
python submit_alpha.py <alpha_id> [alpha_id2 ...]
```

Submit factors to WorldQuant Brain.

Only factors in the `unsubmitted` state can be submitted.

### `add_alpha.py`

```bash
python add_alpha.py <alpha_id> [alpha_id2 ...]
```

Retrieve factor information from WorldQuant Brain and add it to the database.

### `check_correlation.py`

```bash
python check_correlation.py
python check_correlation.py --dry-run
python check_correlation.py --delete-fail
python check_correlation.py --no-notify
```

### `fetch_fields.py`

Retrieve data fields and operators from the WorldQuant Brain API.

* Data fields → `data/fields/`
* Operators → `data/operators/operators.csv`

## Environment Variables

| **Variable**       | **Required** | **Description**           |
| ------------------ | ------------ | ------------------------- |
| `WQ_USERNAME`      | Yes          | WorldQuant Brain username |
| `WQ_PASSWORD`      | Yes          | WorldQuant Brain password |
| `DEEPSEEK_API_KEY` | No           | DeepSeek API Key          |
| `OLLAMA_URL`       | No           | Ollama API address        |
| `OLLAMA_MODEL`     | No           | Ollama model name         |
| `FEISHU_WEBHOOK`   | No           | Feishu bot Webhook URL    |

## Using the DeepSeek API

The DeepSeek API is the recommended choice because:

* No local GPU is required
* Fast response speed
* Supports the DeepSeek model

Get an API Key from the DeepSeek platform.

## Feishu Notifications

After configuring `FEISHU_WEBHOOK`, the system automatically sends notifications when:

* **Alpha Discovered** — Sharpe ≥ 1.25 and Fitness ≥ 1.0
* **Periodic Summary** — Sends a statistical summary after every 100 factors tested
* **Emergency Circuit Breaker** — Alerts after repeated authentication or LLM failures

### Configuration Steps

1. Open a Feishu group
2. Go to Settings → Bot
3. Add a Custom Bot
4. Copy the Webhook URL into `FEISHU_WEBHOOK`

## Feishu Bot Command Control

The Feishu bot allows remote control of the miner, factor-library queries, and factor submission through group messages.

### Environment Variables

```text
FEISHU_APP_ID=your_app_id
FEISHU_APP_SECRET=your_app_secret
```

### Start the Service

```bash
# Run in the foreground
python feishu_bot.py

# Run in the background
nohup python3.11 feishu_bot.py > log/feishu_bot.log 2>&1 &

# Specify a port
python feishu_bot.py --port 8080
```

### Supported Commands

| **Command**              | **Description**                                  | **Example**                 |
| ------------------------ | ------------------------------------------------ | --------------------------- |
| `/summary`               | View factor library statistics and mining status | `/summary`                  |
| `/start [workers]`       | Start the miner                                  | `/start 3`                  |
| `/stop`                  | Stop the miner                                   | `/stop`                     |
| `/check`                 | Run correlation check                            | `/check`                    |
| `/submit <id> [...]`     | Submit specified factors                         | `/submit akornja9 QPnwOqKr` |
| `/list [count] [status]` | View factor list                                 | `/list 50 submitted`        |

### Webhook Configuration

Configure the Feishu Open Platform application to receive:

```text
im.message.receive_v1
```

Request URL:

```text
http://<server-IP>:9000/feishu/webhook
```

### Message Deduplication

The system has a built-in message deduplication mechanism:

* Uses the Feishu `event_id` as the unique identifier
* Cache expires automatically after five minutes
* Cache is cleared after process restart

## Correlation Check

Use `check_correlation.py` to check the `SELF_CORRELATION` status of pending factors.

```bash
# Check all pending factors
python check_correlation.py

# Check only; do not update the database
python check_correlation.py --dry-run

# Keep failed factors instead of deleting them
python check_correlation.py --keep-fail

# Do not send Feishu notifications
python check_correlation.py --no-notify
```

### Scheduled Automatic Checks

Use `setup_cron.sh` to configure daily automatic checks:

```bash
# Add scheduled task
./setup_cron.sh

# Check scheduled task status
./setup_cron.sh status

# Remove scheduled task
./setup_cron.sh remove
```

Scheduled tasks automatically check `SELF_CORRELATION` and send notifications.

## Using Ollama Local Models

If you choose to use Ollama:

```bash
# Install Ollama
curl -fsSL https://ollama.ai/install.sh | sh

# Pull the model
ollama pull qwen3:8b

# Start the service
ollama serve
```

## Team Collaboration

When collaborating with multiple people, each person uses a different `--member-id`:

```bash
# Member A
python run_alpha_miner.py --member-id alice

# Member B
python run_alpha_miner.py --member-id bob
```

The shared factor pool automatically merges factors from all members, and genetic recombination selects from the elite factors across the entire team.

## Troubleshooting

### Authentication Failure

* Confirm that the `.env` file exists
* Confirm that the credentials are correct
* The system automatically retries

### DeepSeek API Error

* Check that `DEEPSEEK_API_KEY` is correct
* Confirm that the API account has sufficient balance

### Ollama Connection Failure

* Confirm that Ollama is running:

```bash
ollama serve
```

* Confirm that the model has been pulled:

```bash
ollama list
```

### API Rate Limit

* The system automatically handles 429 errors
* Reduce the number of `--workers`

### Simulation Timeout

* Default timeout is 10 minutes
* Timed-out simulations are marked as failed
* The next simulation continues

### Submission Failure

* All conditions are checked during submission
* The detailed failure reason is displayed
* Failed factors are automatically deleted from the database

## Logs

Log files are stored in the `log/` directory and rotated daily.

* Current log: `log/YYYY-MM-DD.log`
* A new file is created at midnight
* The most recent 30 days of logs are retained

## Development Notes

### Updating Data Fields

```bash
python fetch_fields.py
```

Fields are automatically saved to `data/fields/` and loaded when the program starts.

### Customizing Alpha Generation Strategies

The following methods in `run_alpha_miner.py` control Alpha generation:

* `generate_alphas()` — New factor generation
* `generate_crossover_alphas()` — Genetic recombination
* `_process_rescue_task()` — Rescue mutation

### Database Structure

#### `alphas` Table

Stores factors that meet the required conditions.

* Primary key: `alpha_id`
* Index: `(expression, region, universe, neutralization)` for deduplication
* Status:

  * `pending`
  * `unsubmitted`
  * `submitted`

#### `rescue_pool` Table

Stores borderline factors that need rescue.

* Primary key: `alpha_id`
* `attempt_count` — Number of rescue attempts, maximum 3
* `failed_checks` — Failed checks used for targeted repairs

## License

MIT License
