"""
WorldQuant Brain Alpha Miner

Simplified alpha mining pipeline using direct API calls.
Supports both Ollama and DeepSeek for LLM-based alpha generation.
"""

import os
import sys
import time
import json
import re
import glob
import queue
import random
import warnings
import threading
import concurrent.futures
from concurrent.futures import ThreadPoolExecutor
from typing import List, Dict

# Suppress SSL verification warnings for WorldQuant Brain API
warnings.filterwarnings('ignore', message='Unverified HTTPS request')

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

from core.config import load_credentials
from core.log_manager import setup_logger
from core.alpha_db import get_alpha_db
from core.submission_quota import get_submission_quota
from core.llm_client import get_llm_client, DEFAULT_SYSTEM_PROMPT
from core.notifier import get_notifier

logger = setup_logger(__name__)

# Constants
BASE_URL = "https://api.worldquantbrain.com"
MIN_SHARPE = 1.25
MIN_FITNESS = 1.0
RESCUE_THRESHOLD = 1.7

# Parameter sweep settings for check failures
SETTINGS_SWEEP = [
    {"neutralization": "INDUSTRY", "truncation": 0.1, "decay": 10, "delay": 1},
    {"neutralization": "INDUSTRY", "truncation": 0.1, "decay": 10, "delay": 0},
    {"neutralization": "SUBINDUSTRY", "truncation": 0.15, "decay": 20, "delay": 1},
    {"neutralization": "SUBINDUSTRY", "truncation": 0.15, "decay": 20, "delay": 0},
    {"neutralization": "SECTOR", "truncation": 0.08, "decay": 30, "delay": 0},
    {"neutralization": "SECTOR", "truncation": 0.08, "decay": 30, "delay": 1},
    {"neutralization": "MARKET", "truncation": 0.2, "decay": 50, "delay": 1},
    {"neutralization": "MARKET", "truncation": 0.2, "decay": 50, "delay": 0},
]

# Check failure strategies
CHECK_STRATEGIES = {
    "TURNOVER": {
        "description": "Turnover rate too high",
        "suggestions": [
            "Increase time-series smoothing window: ts_mean(x, 10) → ts_mean(x, 40)",
            "Increase decay: ts_decay_linear(x, 10) → ts_decay_linear(x, 30)",
            "Use ts_rank instead of zscore to reduce turnover",
            "Double outer shell decay: ts_decay_linear(zscore(...), 10) → ts_decay_linear(zscore(...), 20)"
        ]
    },
    "SELF_CORRELATION": {
        "description": "Self-correlation too high",
        "suggestions": [
            "Change neutralization in settings: INDUSTRY → SUBINDUSTRY/SECTOR/MARKET",
            "Increase truncation: truncation=0.08 → truncation=0.15",
            "Use ts_corr(x, adv20, 20) to introduce volume factor"
        ]
    },
    "DRAWDOWN": {
        "description": "Drawdown too large",
        "suggestions": [
            "Use ts_max(x, 60) to limit maximum drawdown",
            "Increase decay smoothing: ts_decay_linear(x, 20)",
            "Use -1 * x to flip signal direction"
        ]
    }
}

# Rescue decision logic - check types
RESCUABLE_CHECKS = ["TURNOVER", "DRAWDOWN", "TURNOVER_RATE"]
NON_RESCUABLE_CHECKS = ["SELF_CORRELATION", "LOW_SUBMISSION_CORRELATION"]

# Data directories
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
FIELDS_DIR_DELAY1 = os.path.join(DATA_DIR, "fields_delay1")
FIELDS_DIR_DELAY0 = os.path.join(DATA_DIR, "fields_delay0")
OPERATORS_DIR = os.path.join(DATA_DIR, "operators")
SHARED_POOL_DIR = os.path.join(DATA_DIR, "shared_pool")

# Failed pattern blacklist file path
FAILED_PATTERNS_FILE = os.path.join(DATA_DIR, "failed_patterns.json")

# Blacklist threshold: skip after the same pattern fails more than this number of times
FAILED_PATTERN_THRESHOLD = 5

# Upper limit for the number of expression operators
MAX_OPERATORS_COUNT = 64


def clean_expression(expr: str) -> str:
    """Fix common LLM mistakes in expressions."""
    import re

    # Fix scientific notation: 1e-6 → 0.000001 (FASTEXPR does not support e notation)
    def _fix_scientific(m: re.Match) -> str:
        try:
            return format(float(m.group(0)), 'f').rstrip('0').rstrip('.')
        except (ValueError, OverflowError):
            return m.group(0)

    expr = re.sub(r'\b\d+\.?\d*e[+-]?\d+\b', _fix_scientific, expr)

    # WorldQuant Brain uses functional logical operators: and(x,y), or(x,y), not(x)
    # Not infix forms: x and y, x & y
    # Keep as-is for now, as simple regex replacement cannot correctly handle nested logic
    # TODO: Add more complex parsing logic to convert infix forms to functional forms
    return expr

# Target field files to load (API-fetched dataset files)
TARGET_FIELD_FILES = [
    "analyst4.csv",
    "fundamental2.csv",
    "fundamental6.csv",
    "model16.csv",
    "model51.csv",
    "model77.csv",
    "news12.csv",
    "news18.csv",
    "option8.csv",
    "option9.csv",
    "pv1.csv",
    "pv13.csv",
    "sentiment1.csv",
    "socialmedia12.csv",
    "socialmedia8.csv",
    "univ1.csv",
]


class AlphaMiner:
    """Main alpha mining engine with direct API approach."""

    def __init__(self, llm_provider: str = "auto", member_id: str = "default",
                 username: str = None, password: str = None,
                 delay0_prob: float = 0.5):
        self.llm_client = get_llm_client(llm_provider)
        self.alpha_db = get_alpha_db()
        self.quota = get_submission_quota()
        self.member_id = member_id
        self.notifier = get_notifier()
        self._username = username
        self._password = password
        self.delay0_prob = delay0_prob

        # Session state
        self.session = None
        self.tested_expressions = set()
        self._token_expires_at = 0  # Token expiration timestamp
        self._auth_retry_count = 0  # Auth retry count

        # Failed pattern blacklist (persisted to file)
        # Format: {pattern_prefix: {"count": N, "last_seen": timestamp, "example": "..."}}
        self._failed_patterns = self._load_failed_patterns()

        # Queues
        self.llm_task_queue = queue.Queue()
        self.test_queue = queue.Queue()

        # Stats
        self.stats = {
            "tested": 0,
            "passed": 0,
            "failed": 0,
            "rescued": 0,
            "flipped": 0,
            "rescue_pool": 0,
            "best_sharpe": -99.0
        }

        # Dynamic module weights (reinforcement learning style)
        self.module_stats = {
            "ANALYST4": {"tried": 0, "success": 0},
            "FUNDAMENTAL2": {"tried": 0, "success": 0},
            "FUNDAMENTAL6": {"tried": 0, "success": 0},
            "MODEL16": {"tried": 0, "success": 0},
            "MODEL51": {"tried": 0, "success": 0},
            "MODEL77": {"tried": 0, "success": 0},
            "NEWS12": {"tried": 0, "success": 0},
            "NEWS18": {"tried": 0, "success": 0},
            "OPTION8": {"tried": 0, "success": 0},
            "OPTION9": {"tried": 0, "success": 0},
            "PV1": {"tried": 0, "success": 0},
            "PV13": {"tried": 0, "success": 0},
            "SENTIMENT1": {"tried": 0, "success": 0},
            "SOCIALMEDIA12": {"tried": 0, "success": 0},
            "SOCIALMEDIA8": {"tried": 0, "success": 0},
            "UNIV1": {"tried": 0, "success": 0},
        }

        # Operator knowledge base
        self.operator_arity = self._load_operator_knowledge()

        # Create directories
        os.makedirs(DATA_DIR, exist_ok=True)
        os.makedirs(FIELDS_DIR_DELAY1, exist_ok=True)
        os.makedirs(FIELDS_DIR_DELAY0, exist_ok=True)
        os.makedirs(SHARED_POOL_DIR, exist_ok=True)

    def _load_failed_patterns(self) -> Dict:
        """
        Load failed pattern blacklist from file.
        The blacklist is used to skip expression patterns known to fail, avoiding repeated attempts.
        """
        if not os.path.exists(FAILED_PATTERNS_FILE):
            return {}

        try:
            with open(FAILED_PATTERNS_FILE, "r", encoding="utf-8") as f:
                patterns = json.load(f)
                logger.info(f"Loaded {len(patterns)} failed patterns from blacklist")
                return patterns
        except Exception as e:
            logger.warning(f"Failed to load failed patterns: {e}")
            return {}

    def _save_failed_patterns(self):
        """
        Save failed pattern blacklist to file.
        Persists the blacklist so it remains valid after restart.
        """
        try:
            with open(FAILED_PATTERNS_FILE, "w", encoding="utf-8") as f:
                json.dump(self._failed_patterns, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save failed patterns: {e}")

    def _get_pattern_fingerprint(self, expression: str) -> str:
        """
        Get pattern fingerprint of an expression.
        Uses the first 60 characters of the expression as fingerprint to match similar failed patterns.
        """
        # Remove numerical parameters, simplify expression
        import re
        simplified = re.sub(r'\d+', 'N', expression)
        return simplified[:60]

    def is_pattern_blacklisted(self, expression: str) -> bool:
        """
        Check whether expression is in blacklist.
        Returns True if the same pattern has failed more than threshold times, indicating it should be skipped.
        """
        fingerprint = self._get_pattern_fingerprint(expression)
        pattern_info = self._failed_patterns.get(fingerprint)

        if pattern_info and pattern_info.get("count", 0) >= FAILED_PATTERN_THRESHOLD:
            logger.debug(f"Expression in blacklist (failed {pattern_info['count']} times): {fingerprint}...")
            return True

        return False

    def _load_operator_knowledge(self) -> Dict[str, int]:
        """Load operator arity from operators.csv."""
        import pandas as pd
        operators_file = os.path.join(OPERATORS_DIR, "operators.csv")

        if not os.path.exists(operators_file):
            logger.warning(f"Operators file not found: {operators_file}")
            return {}

        try:
            df = pd.read_csv(operators_file, encoding='utf-8-sig')
            # Extract arity from Definition column (count parameters)
            arity_dict = {}
            for _, row in df.iterrows():
                name = str(row.get('Name', '')).strip()
                definition = str(row.get('Definition', ''))
                # Count parameters: look for pattern like func(x, y, z)
                if '(' in definition:
                    params = definition.split('(')[1].split(')')[0]
                    arity = len([p.strip() for p in params.split(',') if p.strip()])
                    arity_dict[name] = arity
            logger.info(f"Loaded {len(arity_dict)} operators")
            return arity_dict
        except Exception as e:
            logger.warning(f"Failed to load operators: {e}")
            return {}

    def validate_expression_variables(self, expression: str, available_fields: set) -> bool:
        """Verify whether all variables in the expression exist in the list of available fields"""
        # Known operators and built-in variables
        known_operators = set(self.operator_arity.keys()) if self.operator_arity else set()
        builtin_vars = {
            'returns', 'volume', 'close', 'open', 'high', 'low', 'vwap',
            'adv20', 'adv50', 'adv120', 'adv240',
            'market_cap', 'sector', 'industry', 'subindustry',
            'liabilities', 'assets', 'equity', 'debt_lt', 'debt_st',
        }

        # Extract all identifiers (variable names) in expression
        # Match identifiers starting with letters and containing letters, digits, underscores
        identifiers = set(re.findall(r'\b([a-zA-Z_][a-zA-Z0-9_]*)\b', expression))

        # Filter out operators and built-in variables
        potential_fields = identifiers - known_operators - builtin_vars

        # Check if each potential field is in the available fields list
        unknown_fields = []
        for field in potential_fields:
            # Skip identifiers starting with digits (possibly constants)
            if field[0].isdigit():
                continue
            # Skip common non-field identifiers
            if field in {'true', 'false', 'null', 'nan', 'inf', 'e', 'pi'}:
                continue
            if field not in available_fields:
                unknown_fields.append(field)

        if unknown_fields:
            logger.warning(f"Expression contains unknown fields: {unknown_fields}")
            return False

        return True

    def authenticate(self) -> bool:
        """Authenticate with WorldQuant Brain API."""
        import requests
        from requests.auth import HTTPBasicAuth
        import time

        username, password = load_credentials()

        logger.info("Authenticating with WorldQuant Brain...")
        self.session = requests.Session()
        self.session.trust_env = False
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json"
        })
        self.session.auth = HTTPBasicAuth(username, password)

        try:
            resp = self.session.post(
                f"{BASE_URL}/authentication",
                verify=False,
                timeout=15
            )
            if resp.status_code == 201:
                logger.info("Authentication successful")
                # Set token expiration time (assuming 2 hours, may actually be shorter)
                self._token_expires_at = time.time() + 7200  # 2 hours
                self._auth_retry_count = 0  # Reset retry count
                return True
            else:
                logger.error(f"Authentication failed: {resp.text}")
                self._auth_retry_count += 1
                return False
        except Exception as e:
            logger.error(f"Authentication error: {e}")
            self._auth_retry_count += 1
            return False

    def _is_token_expired(self) -> bool:
        """Check if token is about to expire (5 minutes in advance)"""
        import time
        return time.time() > (self._token_expires_at - 300)  # 5 minutes in advance

    def _ensure_authenticated(self) -> bool:
        """Ensure authentication is valid; re-authenticate if about to expire"""
        if self._is_token_expired():
            logger.info("Token about to expire, re-authenticating...")
            return self.authenticate()
        return True

    # ==========================================
    # Field Loading (from CSV files)
    # ==========================================

    def load_fields_from_csvs(self, fields_dir: str = None) -> Dict[str, List[Dict]]:
        """Load ALL field data from specified CSV files (full pool for sampling)."""
        import pandas as pd

        # Default to delay1 directory
        if fields_dir is None:
            fields_dir = FIELDS_DIR_DELAY1

        all_fields = {}

        if not os.path.exists(fields_dir):
            logger.info(f"Fields directory not found: {fields_dir}")
            return self._get_default_fields()

        # Get all CSV files in directory
        csv_files = [f for f in os.listdir(fields_dir) if f.endswith('.csv')]

        for file in csv_files:
            filepath = os.path.join(fields_dir, file)
            try:
                df = pd.read_csv(filepath)
                if 'Field' in df.columns and 'Description' in df.columns:
                    # Select required columns
                    columns_to_keep = ['Field', 'Description']
                    if 'Type' in df.columns:
                        columns_to_keep.append('Type')
                    if 'Alphas' in df.columns:
                        columns_to_keep.append('Alphas')

                    fields = df[columns_to_keep].to_dict(orient='records')

                    # Fill default values
                    for field in fields:
                        if 'Type' not in field:
                            field['Type'] = 'MATRIX'
                        if 'Alphas' not in field:
                            field['Alphas'] = 0

                    category = file.replace(".csv", "").upper()
                    all_fields[category] = fields
                    logger.info(f"Loaded {len(fields)} fields from {file}")
            except Exception as e:
                logger.warning(f"Failed to load {file}: {e}")

        if not all_fields:
            logger.info("No target CSV files found, using default fields")
            all_fields = self._get_default_fields()

        return all_fields

    def _get_default_fields(self) -> Dict[str, List[Dict]]:
        """Get default field data for alpha generation."""
        return {
            "PV13": [
                {"Field": "close", "Description": "Closing price"},
                {"Field": "open", "Description": "Opening price"},
                {"Field": "high", "Description": "Highest price"},
                {"Field": "low", "Description": "Lowest price"},
                {"Field": "volume", "Description": "Volume"},
                {"Field": "returns", "Description": "Returns"},
                {"Field": "vwap", "Description": "Volume-weighted average price"}
            ],
            "FUNDAMENTAL6": [
                {"Field": "market_cap", "Description": "Market capitalization"},
                {"Field": "pe_ratio", "Description": "P/E ratio"},
                {"Field": "pb_ratio", "Description": "P/B ratio"},
                {"Field": "roe", "Description": "Return on equity"}
            ]
        }

    # ==========================================
    # Dynamic Module Weights
    # ==========================================

    @staticmethod
    def log_minmax_softmax(values: list, temperature: float = 0.12) -> list:
        """Log + MinMax + Softmax weight transformation.

        1. Log(1+x) compresses extreme values
        2. MinMax normalizes to [0, 1]
        3. Softmax with temperature
        """
        import math
        log_values = [math.log1p(v) for v in values]
        min_val = min(log_values)
        max_val = max(log_values)
        if max_val - min_val == 0:
            return [1.0 / len(values)] * len(values)
        normalized = [(x - min_val) / (max_val - min_val) for x in log_values]
        scaled = [x / temperature for x in normalized]
        max_scaled = max(scaled)
        exp_values = [math.exp(x - max_scaled) for x in scaled]
        sum_exp = sum(exp_values)
        return [v / sum_exp for v in exp_values]

    def get_dynamic_modules(self, fields_pool: Dict, sample_size: int = 15) -> tuple:
        """
        Select 1-2 modules using Log+MinMax+Softmax weighting,
        then sample fields using the same method.
        Returns (selected_fields, modules_used)
        """
        # Calculate total alpha count for each dataset
        module_alpha_counts = {}
        for mod, fields in fields_pool.items():
            total_alphas = sum(f.get('Alphas', 0) for f in fields)
            module_alpha_counts[mod] = total_alphas

        # Calculate dataset weights using Log+MinMax+Softmax
        modules = list(fields_pool.keys())
        raw_counts = [module_alpha_counts.get(mod, 0) for mod in modules]

        if sum(raw_counts) == 0:
            weights = [1.0] * len(modules)
        else:
            weights = self.log_minmax_softmax(raw_counts, temperature=0.12)

        # Low success rate module weight adjustment: lower selection probability for modules with success rate remaining 0 for N consecutive times
        # Maintain exploration, but avoid wasting resources on modules with long-term failure
        LOW_SUCCESS_THRESHOLD = 30  # Consecutive attempt threshold
        LOW_SUCCESS_PENALTY = 0.1   # Weight reduced to 10% of original

        for i, mod in enumerate(modules):
            if mod in self.module_stats:
                tried = self.module_stats[mod]['tried']
                success = self.module_stats[mod]['success']

                # Reduce weight if consecutive attempts exceed threshold and success rate is 0
                if tried >= LOW_SUCCESS_THRESHOLD and success == 0:
                    weights[i] *= LOW_SUCCESS_PENALTY
                    logger.debug(f"Module {mod} weight reduced (tried={tried}, success={success})")

        # Select 1 or 2 modules
        num_to_select = random.choice([1, 2])
        selected = random.choices(modules, weights=weights, k=num_to_select)
        selected = list(set(selected))

        # Within selected datasets, select fields using Log+MinMax+Softmax
        selected_fields = {}
        for mod in selected:
            pool = fields_pool.get(mod, [])
            if not pool:
                continue

            raw_weights = [f.get('Alphas', 0) for f in pool]
            if sum(raw_weights) == 0:
                field_weights = [1.0] * len(pool)
            else:
                field_weights = self.log_minmax_softmax(raw_weights, temperature=0.12)

            n = min(sample_size, len(pool))
            selected_fields[mod] = random.choices(pool, weights=field_weights, k=n)

        return selected_fields, selected

    def record_module_stat(self, modules_used: List[str], success: bool):
        """Record success/failure for module weight updates."""
        for mod in modules_used:
            if mod in self.module_stats:
                self.module_stats[mod]['tried'] += 1
                if success:
                    self.module_stats[mod]['success'] += 1

    # ==========================================
    # Shared Pool Management
    # ==========================================

    def load_shared_pool(self) -> List[Dict]:
        """Load and merge all shared pool files."""
        combined_pool = []
        search_pattern = os.path.join(SHARED_POOL_DIR, "shared_pool_*.json")

        for file_path in glob.glob(search_pattern):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    combined_pool.extend(json.load(f))
            except Exception:
                continue

        # Sort by Sharpe and keep top 500
        combined_pool.sort(key=lambda x: x.get('sharpe', 0), reverse=True)
        return combined_pool[:500]

    def add_to_shared_pool(self, expression: str, sharpe: float, fitness: float, logic: str = ""):
        """Add factor to member's shared pool file."""
        my_pool_path = os.path.join(SHARED_POOL_DIR, f"shared_pool_{self.member_id}.json")

        my_pool = []
        if os.path.exists(my_pool_path):
            try:
                with open(my_pool_path, "r", encoding="utf-8") as f:
                    my_pool = json.load(f)
            except Exception:
                pass

        my_pool.append({
            "expression": expression,
            "sharpe": sharpe,
            "fitness": fitness,
            "logic": logic
        })

        # Sort and keep top 500
        my_pool.sort(key=lambda x: x.get('sharpe', 0), reverse=True)
        my_pool = my_pool[:500]

        with open(my_pool_path, "w", encoding="utf-8") as f:
            json.dump(my_pool, f, ensure_ascii=False, indent=2)

    # ==========================================
    # Alpha Generation
    # ==========================================

    def generate_alphas(self, fields_data: Dict = None) -> List[Dict]:
        """Generate alpha expressions using LLM with dynamic module selection."""
        # Select delay0 or delay1 fields based on probability
        if random.random() < self.delay0_prob:
            fields_data = self.fields_delay0
            delay = 0
            logger.info("Selected delay=0 fields")
        else:
            fields_data = self.fields_delay1
            delay = 1
            logger.info("Selected delay=1 fields")

        # Use dynamic module selection
        target_fields, modules_used = self.get_dynamic_modules(fields_data)
        mod_names = "+".join(modules_used)

        logger.info(f"Generating alphas for modules: {mod_names}")

        # Separate MATRIX and VECTOR fields
        matrix_fields = {}
        vector_fields = {}
        for mod, fields in target_fields.items():
            matrix_list = [f for f in fields if f.get('Type', 'MATRIX') == 'MATRIX']
            vector_list = [f for f in fields if f.get('Type', 'MATRIX') == 'VECTOR']
            if matrix_list:
                matrix_fields[mod] = matrix_list
            if vector_list:
                vector_fields[mod] = vector_list

        # Build field descriptions
        field_description = "【MATRIX FIELDS】(Can normally use all operators):\n"
        field_description += json.dumps(matrix_fields, ensure_ascii=False)

        if vector_fields:
            field_description += "\n\n【⚠️ FORBIDDEN VECTOR FIELDS — WQ API rejects all operators】:\n"
            field_description += json.dumps(vector_fields, ensure_ascii=False)
            field_description += """
VECTOR fields are strictly forbidden:
- ❌ == != > < >= <= all error "does not support event inputs"
- ❌ sign(), trade_when(), rank(), zscore() all error
- ❌ Cannot participate in any arithmetic operations (+,-,*,/)
- ❌ Cannot be used in ts_delta, ts_mean, ts_sum, if_else, or anywhere else
- ⚠️ Only use the MATRIX fields above! VECTOR fields are listed only so you know NOT to use them!"""

        prompt = f"""Please use the provided data fields below to generate 5 explosive new factors.

【CRITICAL RULES】
1. You MUST ONLY use the field names listed below, absolutely NO other field names!
2. Do NOT use non-existent fields like total_assets, book_value_per_share, return_on_equity, etc.
3. If total assets are needed, use assets; if equity is needed, use equity; if long-term debt is needed, use debt_lt
4. Logical operators MUST use functional syntax: and(x,y), or(x,y), not(x); infix forms or & | ~ are FORBIDDEN
5. The current data field delay is delay={delay}, delay in settings for all generated factors MUST be set to {delay}

【AVOID COMMON FAILURE PATTERNS】
- High turnover (HIGH_TURNOVER): Use a larger decay window (ts_decay_linear(x, 20) or larger) to avoid overly sensitive signals
- Concentrated weights (CONCENTRATED_WEIGHT): Use zscore or rank for normalization to avoid extreme weights
- Low sub-universe Sharpe (LOW_SUB_UNIVERSE_SHARPE): Use industry or sector neutralization to avoid over-reliance on specific stocks

【RECOMMENDED FACTOR STRUCTURE】
1. Outer shell: ts_decay_linear(zscore(...), 20)  # Use larger decay window to reduce turnover
2. Core logic: Use normalization functions like rank, zscore, ts_mean
3. Neutralization: Set neutralization: "INDUSTRY" or "SUBINDUSTRY" in settings
4. Truncation: Set truncation: 0.08 or 0.1 in settings

{field_description}"""

        results = self.llm_client.generate_alphas(DEFAULT_SYSTEM_PROMPT, prompt)

        if results:
            self.notifier.record_llm_success()
        else:
            self.notifier.record_llm_error()

        # Build available field set (for validation)
        available_fields = set()
        for mod, fields in target_fields.items():
            for field in fields:
                available_fields.add(field['Field'])

        # Clean expressions, validate variables, and tag with modules used
        valid_results = []
        for res in results:
            expression = res.get('expression', '')
            if not expression:
                continue

            # Validate variables
            if not self.validate_expression_variables(expression, available_fields):
                logger.warning(f"Skipping expression with unknown variables: {expression[:60]}...")
                continue

            # Validate expression quality
            if not self._validate_expression_quality(expression):
                logger.warning(f"Skipping low quality expression: {expression[:60]}...")
                continue

            res['expression'] = clean_expression(expression)
            res['modules_used'] = modules_used
            res['delay'] = delay  # Add delay value
            valid_results.append(res)

        return valid_results

    def _count_operators(self, expression: str) -> int:
        """
        Count the number of operators in the expression.
        Operators include: function calls, brackets, commas, etc.
        Uses a simplified counting method: count of left parentheses + count of commas.
        """
        # Count left parentheses (each left parenthesis represents a function call or sub-expression)
        left_parens = expression.count('(')

        # Count commas (each comma represents a parameter delimiter)
        commas = expression.count(',')

        # Operator count roughly equals left parens + commas
        # This is a simplified calculation, but sufficient for filtering overly long expressions
        return left_parens + commas

    def _validate_expression_quality(self, expression: str) -> bool:
        """
        Validate expression quality to filter out low-quality expressions early.
        Returns True if quality is qualified, False if it should be skipped.
        """
        # Check expression length (too short may indicate incomplete logic)
        if len(expression) < 20:
            return False

        # Check if operator count exceeds limit
        operator_count = self._count_operators(expression)
        if operator_count > MAX_OPERATORS_COUNT:
            logger.warning(f"Expression with {operator_count} operators exceeds limit of {MAX_OPERATORS_COUNT}: {expression[:60]}...")
            return False

        # Check if basic normalization functions are included
        has_normalization = any(func in expression for func in ['zscore', 'rank', 'ts_rank', 'ts_zscore'])
        if not has_normalization:
            return False

        # Check if time-series smoothing is included
        has_smoothing = any(func in expression for func in ['ts_decay_linear', 'ts_mean', 'ts_std_dev'])
        if not has_smoothing:
            return False

        # Check for excessive nesting (which may cause high computational complexity)
        nesting_depth = 0
        max_nesting = 0
        for char in expression:
            if char == '(':
                nesting_depth += 1
                max_nesting = max(max_nesting, nesting_depth)
            elif char == ')':
                nesting_depth -= 1

        # If nesting depth exceeds 8 levels, it may be overly complex
        if max_nesting > 8:
            return False

        # Check for common error patterns
        error_patterns = [
            'ts_min',  # Non-existent function
            'ts_max',  # Non-existent function
            'group_neutralize',  # Should not be in expression
            '"',  # Quotes should not exist
            "'",  # Quotes should not exist
        ]

        for pattern in error_patterns:
            if pattern in expression:
                return False

        return True

    def _record_failed_pattern(self, expression: str, failed_checks: list):
        """
        Record failed pattern to blacklist to prevent generating similar expressions repeatedly.
        Once a pattern fails more than the threshold times, it will be skipped and no longer attempted.
        """
        import time

        # Get pattern key using fingerprint method
        pattern_key = self._get_pattern_fingerprint(expression)

        if pattern_key not in self._failed_patterns:
            self._failed_patterns[pattern_key] = {
                "count": 0,
                "checks": [],
                "example": expression[:80],
                "last_seen": time.time()
            }

        self._failed_patterns[pattern_key]["count"] += 1
        self._failed_patterns[pattern_key]["last_seen"] = time.time()

        # Update failure check types (convert to list to support JSON serialization)
        existing_checks = set(self._failed_patterns[pattern_key].get("checks", []))
        existing_checks.update(failed_checks)
        self._failed_patterns[pattern_key]["checks"] = list(existing_checks)

        # Log warning
        count = self._failed_patterns[pattern_key]["count"]
        if count >= FAILED_PATTERN_THRESHOLD:
            logger.warning(f"Pattern added to blacklist (failed {count} times): {pattern_key}... "
                           f"checks: {failed_checks}")
            # Save blacklist to file
            self._save_failed_patterns()
        elif count >= 3:
            logger.warning(f"Repeated failure pattern detected: {pattern_key}... "
                           f"(failed {count} times, checks: {failed_checks})")

    def generate_crossover_alphas(self) -> List[Dict]:
        """Generate alphas by crossing over elite factors from shared pool using non-linear operations."""
        pool = self.load_shared_pool()

        if len(pool) < 2:
            logger.info("Not enough factors in shared pool for crossover")
            return []

        # Select 2 random elite parents
        parents = random.sample(pool, 2)

        logger.info("Generating crossover from elite parents (non-linear)...")

        prompt = f"""Two submitted factors; linear combinations are FORBIDDEN (cannot pass correlation checks).

【STEPS】
1. Extract core logic of parent factors (strip outer shell ts_decay_linear(zscore(...)))
2. Hybridize using the following non-linear methods:
   - ts_corr(core_A, core_B, d): compute time-series correlation
   - ts_cov(core_A, core_B, d): compute time-series covariance
   - rank(core_A) / rank(core_B): rank ratio
   - sign(core_A) * abs(core_B): sign combination
   - Swap data fields: retain Parent A structure, replace with Parent B fields
   - Swap operators: ts_mean↔ts_std_dev, ts_rank↔ts_zscore
3. Re-apply outer shell

【PARENT FACTORS】
Parent A: {parents[0]['expression']}
Parent B: {parents[1]['expression']}

【OUTPUT REQUIREMENTS】
Output 3 variants, outer shell MUST be: ts_decay_linear(zscore(...), 10)
Neutralization is controlled by settings; do NOT include group_neutralize in the expression"""

        results = self.llm_client.generate_alphas(DEFAULT_SYSTEM_PROMPT, prompt)

        if results:
            self.notifier.record_llm_success()
        else:
            self.notifier.record_llm_error()

        # Clean expressions, validate quality, and tag as crossover
        valid_results = []
        for res in results:
            expression = res.get('expression', '')
            if not expression:
                continue

            # Validate expression quality
            if not self._validate_expression_quality(expression):
                logger.warning(f"Skipping low quality crossover expression: {expression[:60]}...")
                continue

            res['expression'] = clean_expression(expression)
            res['modules_used'] = []  # Crossover doesn't count for module stats
            valid_results.append(res)

        return valid_results

    # ==========================================
    # Simulation & Polling
    # ==========================================

    def simulate_factor(self, factor: Dict) -> Dict:
        """Submit factor for backtesting and poll for results."""
        # Ensure authentication is valid
        if not self._ensure_authenticated():
            return {"error": "AUTH_FAILED"}

        if not self.session:
            return {"error": "Not authenticated"}

        expression = factor.get("expression", "")
        if not expression:
            return {"error": "Empty expression"}

        # Build simulation settings
        settings = {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": "TOP3000",
            "delay": factor.get("delay", 1),  # Use delay value from factor
            "decay": 0,
            "neutralization": "INDUSTRY",
            "truncation": 0.08,
            "pasteurization": "ON",
            "unitHandling": "VERIFY",
            "nanHandling": "OFF",
            "language": "FASTEXPR",
            "visualization": False
        }

        # Apply custom settings if provided (delay is system controlled, not overridden by LLM)
        actual_delay = factor.get("delay", 1)
        if isinstance(factor.get("settings"), dict):
            for k, v in factor["settings"].items():
                if k in settings and k != "delay":
                    settings[k] = v
        # Ensure again delay uses system-set value, not overridden by LLM output settings
        settings["delay"] = actual_delay

        payload = {
            "type": "REGULAR",
            "s
