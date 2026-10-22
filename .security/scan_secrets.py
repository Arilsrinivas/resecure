#!/usr/bin/env python3
"""
ReSecure Security Audit - Secret Scanning Engine
Scans repository files and git history for credentials, API keys, private keys, and high-entropy secrets.
"""

import math
import os
import re
import subprocess
import sys
from pathlib import Path

# Common high-risk secret patterns
PATTERNS = {
    "RSA/OpenSSH Private Key": re.compile(r"-----BEGIN (?:RSA|DSA|EC|OPENSSH|PGP) PRIVATE KEY"),
    "Generic Private Key": re.compile(r"-----BEGIN PRIVATE KEY-----"),
    "AWS Access Key ID": re.compile(r"\b(AKIA[0-9A-Z]{16})\b"),
    "AWS Secret Access Key": re.compile(r"(?i)aws_secret_access_key\s*[:=]\s*['\"]?([A-Za-z0-9/+=]{40})['\"]?"),
    "GitHub Personal Access Token": re.compile(r"\b(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82})\b"),
    "Generic Bearer Token": re.compile(r"(?i)bearer\s+([a-zA-Z0-9_\-\.]{30,})"),
    "Google API Key": re.compile(r"\b(AIza[0-9A-Za-z\\-_]{35})\b"),
    "Stripe Secret Key": re.compile(r"\b(sk_live_[0-9a-zA-Z]{24})\b"),
    "Slack Token": re.compile(r"\b(xox[baprs]-[0-9a-zA-Z]{10,48})\b"),
    "JWT Token": re.compile(r"\beyJ[A-Za-z0-9-_]+\.eyJ[A-Za-z0-9-_]+\.[A-Za-z0-9-_]+\b"),
    "Hardcoded Password Assignment": re.compile(r"(?i)(?:password|passwd|pwd|secret_key)\s*=\s*['\"][^'\"]{6,}['\"]"),
}

# Paths to exclude from scanning (e.g. git internal objects, node_modules)
EXCLUDE_DIRS = {".git", "node_modules", ".pytest_cache", "__pycache__", "dist", "build"}
EXCLUDE_FILES = {"package-lock.json"}

def shannon_entropy(data: str) -> float:
    if not data:
        return 0.0
    entropy = 0.0
    for x in set(data):
        p_x = float(data.count(x)) / len(data)
        if p_x > 0:
            entropy += - p_x * math.log2(p_x)
    return entropy

def scan_files(root_dir: str):
    findings = []
    root = Path(root_dir)
    total_files = 0
    
    for current_dir, dirs, files in os.walk(root):
        # Exclude directories
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for f in files:
            if f in EXCLUDE_FILES:
                continue
            file_path = Path(current_dir) / f
            total_files += 1
            try:
                content = file_path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
                
            for rule_name, pattern in PATTERNS.items():
                for match in pattern.finditer(content):
                    matched_str = match.group(0)
                    # Filter out template placeholders or benign examples
                    if "replace-with" in matched_str or "example" in matched_str.lower() or "your_" in matched_str.lower():
                        continue
                    line_no = content[:match.start()].count("\n") + 1
                    findings.append({
                        "file": str(file_path.relative_to(root)),
                        "line": line_no,
                        "rule": rule_name,
                        "match_preview": matched_str[:80],
                    })
    return total_files, findings

def scan_git_history(repo_dir: str):
    findings = []
    try:
        # Run git log -p to inspect full commit diffs
        cmd = ["git", "log", "-p", "--no-color", "-n", "100"]
        result = subprocess.run(cmd, cwd=repo_dir, capture_output=True, text=True, errors="ignore")
        diff_text = result.stdout
        
        current_commit = "unknown"
        for line in diff_text.splitlines():
            if line.startswith("commit "):
                current_commit = line.split()[1]
            if line.startswith("+") and not line.startswith("+++"):
                added_line = line[1:].strip()
                for rule_name, pattern in PATTERNS.items():
                    if rule_name in ("Hardcoded Password Assignment", "Generic Bearer Token"):
                        continue # Skip looser patterns in diffs to avoid noise
                    match = pattern.search(added_line)
                    if match:
                        matched_str = match.group(0)
                        if "replace-with" in matched_str or "example" in matched_str.lower():
                            continue
                        findings.append({
                            "commit": current_commit,
                            "rule": rule_name,
                            "line": added_line[:100]
                        })
    except Exception as e:
        print(f"Error checking git history: {e}")
    return findings

if __name__ == "__main__":
    repo_root = os.path.abspath(".")
    print(f"=== ReSecure Secret Scanning Engine ===")
    print(f"Scanning directory: {repo_root}")
    total_files, file_findings = scan_files(repo_root)
    print(f"Total files scanned: {total_files}")
    print(f"File secret findings: {len(file_findings)}")
    for f in file_findings:
        print(f"  [ALERT] {f['file']}:{f['line']} - {f['rule']}: {f['match_preview']}")
        
    print("\nScanning Git commit history...")
    git_findings = scan_git_history(repo_root)
    print(f"Git history secret findings: {len(git_findings)}")
    for g in git_findings:
        print(f"  [GIT ALERT] Commit {g['commit'][:8]} - {g['rule']}: {g['line']}")
