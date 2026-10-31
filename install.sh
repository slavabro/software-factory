#!/bin/sh
# Install the software factory on this machine (macOS or Linux; idempotent; needs python3 >= 3.11).
#   git clone git@github.com:slavabro/software-factory.git ~/Dev/software-factory && ~/Dev/software-factory/install.sh
# Links, never copies, so `git pull` here updates everything:
#   plugins/factory  -> ~/.claude/skills/factory   (a skills-dir plugin: loads live as factory@skills-dir)
#   home/skills/*    -> ~/.claude/skills/*          (user skills that keep their bare /name, e.g. /fleet)
#   home/bin/*       -> ~/.local/bin/*              (skillcheck, tower)
set -eu
ROOT=$(cd "$(dirname "$0")" && pwd)

python3 -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null \
  || { echo "install: python3 >= 3.11 is required" >&2; exit 1; }
mkdir -p "$HOME/.claude/skills" "$HOME/.local/bin"

link() {  # link SRC DST — replace an old link; move a real file or directory to ~/.claude/backups first
  if [ -e "$2" ] && [ ! -L "$2" ]; then  # (a backup left in ~/.claude/skills would load as a duplicate skill)
    mkdir -p "$HOME/.claude/backups"
    bak="$HOME/.claude/backups/$(basename "$2").$(date +%Y%m%d%H%M%S)"
    mv "$2" "$bak" && echo "backed up $2 -> $bak"
  fi
  ln -sfn "$1" "$2" && echo "linked $2 -> $1"
}

link "$ROOT/plugins/factory" "$HOME/.claude/skills/factory"
for d in "$ROOT"/home/skills/*/; do
  name=$(basename "$d")
  link "$ROOT/home/skills/$name" "$HOME/.claude/skills/$name"
done
for f in "$ROOT"/home/bin/*; do
  link "$f" "$HOME/.local/bin/$(basename "$f")"
done

# User skills config: skip vendor stubs that their own installers overwrite (Orca links them from ~/.agents/skills).
cfg="$HOME/.claude/skills/.skillcheck.toml"
[ -e "$cfg" ] || printf '%s\n' '# skillcheck config for ~/.claude/skills (written by software-factory/install.sh)' \
  'exclude = ["orca-cli/**", "orchestration/**"]' > "$cfg"

case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "note: add ~/.local/bin to PATH" ;; esac
"$HOME/.local/bin/skillcheck" --scope user "$HOME/.claude/skills" || true
echo "done — new Claude Code sessions load factory@skills-dir (skillcheck hook + /factory:skills-review)"
