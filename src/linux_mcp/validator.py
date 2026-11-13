import re
import shlex
from typing import Tuple

# Commands that are explicitly allowed (read/diagnostic only)
ALLOWED_COMMANDS = {
    # System info
    "uname", "hostname", "uptime", "date", "whoami", "id",
    # Hardware & resources
    "df", "du", "free", "lscpu", "lsmem", "lsblk", "lspci", "lsusb", "lshw",
    "dmidecode", "hwinfo",
    # Processes
    "ps", "top", "htop", "pidstat", "pstree", "lsof",
    # Network
    "ip", "ifconfig", "netstat", "ss", "ping", "traceroute", "nslookup",
    "dig", "host", "arp", "route", "iptables -L", "nmap",
    # Logs & files (read only)
    "cat", "less", "more", "head", "tail", "grep", "find", "locate",
    "stat", "file", "ls", "tree", "wc", "diff", "md5sum", "sha256sum",
    # OS & packages
    "dpkg", "rpm", "apt list", "yum list", "snap list",
    "systemctl status", "systemctl list-units", "service --status-all",
    # Disk & filesystem
    "mount", "findmnt", "blkid", "fdisk -l", "pvs", "vgs", "lvs",
    # Users & auth
    "who", "w", "last", "lastlog", "getent", "groups",
    # Misc diagnostics
    "env", "printenv", "ulimit", "sysctl", "dmesg", "journalctl",
    "crontab -l", "sar", "vmstat", "iostat", "mpstat",
}

# Patterns that are ALWAYS blocked regardless of context
BLOCKED_PATTERNS = [
    # Write / delete operations
    r'\brm\b', r'\brmdir\b', r'\bmv\b', r'\bcp\b(?=.*>)',
    r'\btouch\b', r'\bmkdir\b', r'\bchmod\b', r'\bchown\b',
    r'\bchattr\b', r'\bsetfacl\b', r'\bln\b',
    # File writing via redirection
    r'\s>\s', r'\s>>', r'\btee\b', r'\btruncate\b',
    # Package management (write)
    r'\bapt\s+(install|remove|purge|upgrade|autoremove)\b',
    r'\byum\s+(install|remove|update|erase)\b',
    r'\bdnf\s+(install|remove|update)\b',
    r'\bpip\s+(install|uninstall)\b',
    r'\bnpm\s+(install|uninstall|update)\b',
    # Service control
    r'\bsystemctl\s+(start|stop|restart|enable|disable|mask|reload)\b',
    r'\bservice\s+\w+\s+(start|stop|restart|reload)\b',
    r'\binit\b', r'\btelinit\b',
    # User management
    r'\buseradd\b', r'\buserdel\b', r'\busermod\b',
    r'\bgroupadd\b', r'\bgroupdel\b', r'\bpasswd\b',
    r'\bchpasswd\b', r'\bnewusers\b',
    # Network changes
    r'\bifconfig\s+\w+\s+(up|down)\b', r'\bip\s+link\s+set\b',
    r'\bip\s+addr\s+(add|del)\b', r'\bip\s+route\s+(add|del)\b',
    r'\biptables\s+(-A|-D|-I|-F|-X|-P)\b', r'\bufw\s+(enable|disable|allow|deny)\b',
    # Dangerous execution
    r'\bsudo\b', r'\bsu\b(?:\s|$)', r'\bdoas\b',
    r'\bchroot\b', r'\bnsenter\b',
    r'\bkill\b', r'\bkillall\b', r'\bpkill\b',
    r'\bshutdown\b', r'\breboot\b', r'\bhalt\b', r'\bpoweroff\b',
    # Code execution / downloads
    r'\bcurl\b.*(-o|--output|sh\b)', r'\bwget\b.*(-O|sh\b)',
    r'\bbash\s+-c\b', r'\bsh\s+-c\b', r'\beval\b', r'\bexec\b',
    r'\bpython\b.*-c', r'\bperl\b.*-e', r'\bruby\b.*-e',
    # Cron / scheduler changes
    r'\bcrontab\s+-[eri]\b',
    # Disk operations
    r'\bdd\b', r'\bfdisk\b(?!\s+-l)', r'\bparted\b', r'\bmkfs\b',
    r'\bfsck\b', r'\bformat\b', r'\bshred\b', r'\bwipe\b',
    # Mount changes
    r'\bmount\b(?!\s+$|\s+-l|\s+--list)', r'\bumount\b',
    # Kernel / modules
    r'\binsmod\b', r'\brmmod\b', r'\bmodprobe\b(?!\s+-l)',
    r'\bsysctl\s+-w\b',
    # Chaining evasion attempts
    r';\s*(?:rm|dd|mkfs|shutdown|reboot)',
    r'\|\s*(?:bash|sh)',
    r'`[^`]*`',  # backtick execution
    r'\$\([^)]*\)',  # $() subshell execution
]


def validate_read_command(command: str) -> Tuple[bool, str]:
    """
    Validate a command for the READ-ONLY tool.
    Returns (is_allowed: bool, reason: str)
    """
    command = command.strip()

    # 1. Block empty commands
    if not command:
        return False, "Empty command"

  
    # 2. Block multi-line commands (potential obfuscation)
    '''
    if '\n' in command:
        return False, "Multi-line commands are not allowed"
    '''

    # 3. Check against all blocked patterns
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Blocked pattern detected: '{pattern}'"

    # 4. Extract the base command (first word) and check whitelist
    base_command = command.split()[0].lstrip("./")
    if base_command not in ALLOWED_COMMANDS:
        return False, f"Command '{base_command}' is not in the allowed list"
  

    return True, "Command approved"


# ══════════════════════════════════════════════════════════════════════════
# WRITE validation — used only for servers with allow_write: true.
# Normal admin work is allowed; only catastrophic / lock-out commands are blocked.
# ══════════════════════════════════════════════════════════════════════════

# Paths that must never be the target of recursive delete / chmod / chown / mv
_PROTECTED_LITERALS = {"/", "/*", "~", "~/", "~/*", "*", ".", "./", "./*", "..", "../", "../*"}
_PROTECTED_DIR_RE = re.compile(
    r"/+(etc|usr|bin|sbin|boot|var|lib|lib32|lib64|home|root|opt|dev|proc|sys|srv|run)/*\*?"
)

HARMFUL_PATTERNS = [
    # Obfuscation / nested execution
    (r'`[^`]*`', "backtick execution"),
    (r'\$\([^)]*\)', "$() subshell execution"),
    (r'\bbash\s+-c\b|\bsh\s+-c\b|\bzsh\s+-c\b', "shell -c execution"),
    (r'\beval\b', "eval"),
    (r'\bexec\b', "exec"),
    (r'\bpython[0-9.]*\b.*\s-c\b|\bperl\b.*\s-e\b|\bruby\b.*\s-e\b', "inline interpreter code"),
    (r'\b(curl|wget)\b[^;&]*\|\s*(sudo\s+)?(ba|z|k|da)?sh\b', "download piped to shell"),
    (r'\|\s*(sudo|bash|sh|zsh)\b', "pipe to shell"),
    (r':\(\)\s*\{', "fork bomb"),
    # Privilege escalation
    (r'\bsudo\b|\bsu\b(?:\s|$)|\bdoas\b|\bpkexec\b|\bchroot\b|\bnsenter\b', "privilege escalation / namespace change"),
    # Recursive delete safety switch
    (r'--no-preserve-root', "--no-preserve-root"),
    (r'\bfind\s+/+(\s|$)[^;&|]*-delete\b', "find / -delete"),
    # Disk / filesystem destruction
    (r'\bmkfs(\.\w+)?\b|\bmke2fs\b|\bwipefs\b', "filesystem format / wipe"),
    (r'\bdd\b[^;&|]*\bof=/dev/', "dd writing to a device"),
    (r'>\s*/dev/(sd|hd|vd|xvd|nvme|mmcblk|dm-|md|mapper|disk)', "redirect to a block device"),
    (r'\bshred\b', "shred"),
    (r'\bfdisk\b(?!\s+-l)|\bsfdisk\b|\bparted\b|\bgdisk\b', "partition table changes"),
    (r'\b(pvremove|vgremove|lvremove)\b', "LVM volume removal"),
    # Power state
    (r'\bshutdown\b|\breboot\b|\bhalt\b|\bpoweroff\b|\btelinit\b|\binit\s+[0156]\b', "power state change"),
    (r'\bsystemctl\s+(reboot|poweroff|halt|kexec|rescue|emergency|isolate)\b', "power state change"),
    # Accounts and authentication
    (r'\b(passwd|chpasswd|useradd|userdel|usermod|groupdel|newusers|visudo)\b', "user / password management"),
    (r'/etc/(shadow|gshadow|passwd|sudoers)', "editing authentication files"),
    # Remote access lock-out
    (r'\biptables\b[^;&|]*(\s-F\b|--flush\b|\s-X\b|-P\s+\w+\s+DROP\b)', "firewall flush / default DROP"),
    (r'\bip\s+link\s+set\b[^;&|]*\bdown\b|\bifdown\b|\bifconfig\s+\S+\s+down\b', "bringing a network interface down"),
    (r'\bnmcli\b[^;&|]*\b(down|off)\b', "bringing a network connection down"),
    (r'\bsystemctl\s+(stop|disable|mask|kill)\s+(sshd?|network|NetworkManager)\b', "stopping SSH / networking"),
    (r'\b(pkill|killall)\b[^;&|]*\bsshd?\b', "killing SSH"),
    (r'\bkillall5\b', "killall5"),
    # Scheduler wipe
    (r'\bcrontab\s+-r\b', "removing all cron jobs"),
]


def _segments(command: str) -> list[list[str]]:
    """Split a command line into simple-command token lists (on ; & | && ||)."""
    result = []
    for part in re.split(r'[;&|]+', command):
        try:
            tokens = shlex.split(part)
        except ValueError:
            tokens = part.split()
        if tokens:
            result.append(tokens)
    return result


def _is_protected_path(token: str) -> bool:
    return token in _PROTECTED_LITERALS or bool(_PROTECTED_DIR_RE.fullmatch(token))


def _check_tokens(tokens: list[str]) -> str | None:
    for i, tok in enumerate(tokens):
        prog = tok.rsplit("/", 1)[-1]
        args = tokens[i + 1:]
        flags = [a for a in args if a.startswith("-")]
        targets = [a for a in args if not a.startswith("-")]
        recursive = any(
            f in ("--recursive",) or (not f.startswith("--") and ("r" in f or "R" in f))
            for f in flags
        )

        if prog == "rm" and recursive and any(_is_protected_path(t) for t in targets):
            return "recursive delete of a root / system path"
        if prog in ("chmod", "chown", "chgrp") and recursive and any(_is_protected_path(t) for t in targets):
            return f"recursive {prog} on a root / system path"
        if prog == "mv" and any(_is_protected_path(t) for t in targets):
            return "moving a root / system path"
        if prog == "kill":
            if "1" in targets or (len(args) > 1 and args[-1] == "-1"):
                return "killing init / all processes"
    return None


def validate_write_command(command: str) -> Tuple[bool, str]:
    """
    Validate a command for the WRITE tool.
    Returns (is_allowed: bool, reason: str)
    """
    command = command.strip()

    if not command:
        return False, "Empty command"

    # if '\n' in command or '\r' in command:
    #    return False, "Multi-line commands are not allowed"

    for pattern, label in HARMFUL_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return False, f"Harmful command detected: {label}"

    for tokens in _segments(command):
        reason = _check_tokens(tokens)
        if reason:
            return False, f"Harmful command detected: {reason}"

    return True, "Command approved"
