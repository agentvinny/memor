# memor shell hook (zsh) — capture every interactive command as raw evidence.
#
# Install: add one line to ~/.zshrc
#   source /home/adlokib/work/projects/memory/hooks/memor-zsh-hook.zsh
#
# The hook evals a snippet printed by `memor shell-hook zsh`, which registers
# preexec/precmd handlers recording command + exit code + cwd + timing to the
# memor store (env-var handoff, no quoting hazards).
#
# Silence it in a shell:  export MEMOR_DISABLED=1
#
_MEMOR_HOOK_BIN=""
for _c in "${MEMOR_BIN:-}" "$(command -v memor 2>/dev/null)" \
          "$HOME/work/projects/memory/.venv/bin/memor" "$HOME/.local/bin/memor"; do
  if [ -n "$_c" ] && [ -x "$_c" ]; then _MEMOR_HOOK_BIN="$_c"; break; fi
done
if [ -n "$_MEMOR_HOOK_BIN" ] && [ -z "${MEMOR_DISABLED:-}" ]; then
  eval "$("$_MEMOR_HOOK_BIN" shell-hook zsh)"
fi
unset _c _MEMOR_HOOK_BIN