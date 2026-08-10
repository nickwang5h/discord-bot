# Project-local VS Code zsh entry point.
# VS Code initially sets ZDOTDIR here so zsh loads this tracked file.

project_workspace_root="${PROJECT_WORKSPACE_ROOT:-${PWD}}"
project_user_zdotdir="${PROJECT_USER_ZDOTDIR:-${HOME}}"

# Restore the user configuration directory before loading Oh My Zsh or other
# completion frameworks, keeping history and completion dumps out of the project.
export ZDOTDIR="$project_user_zdotdir"
[[ -r "$ZDOTDIR/.zshrc" ]] && source "$ZDOTDIR/.zshrc"

# Add optional project-only shell setup in this tracked file.
if [[ -r "$project_workspace_root/.vscode/zsh/project.zsh" ]]; then
  source "$project_workspace_root/.vscode/zsh/project.zsh"
fi

unset project_workspace_root project_user_zdotdir
