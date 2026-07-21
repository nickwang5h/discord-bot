# Agent Behavior Rules for Discord Bot Project

## 1. Skill Enforcement
**CRITICAL**: When starting a new conversation, or when asked to modify ANY code in this repository, you **MUST** first review the available custom skills in the workspace.
In particular, you **MUST** read and strictly follow the `maintain-architecture` skill (`.agents/skills/maintain-architecture/SKILL.md`) **BEFORE** writing any code, planning an implementation, or modifying files.

Do not skip reading `arch.md`, testing, and updating `arch.md` as mandated by the skill. Failure to do this will result in architectural divergence.

## 2. Hard Requirements Before Modifying Code
**CRITICAL RULE**: 以后在这个项目里，任何涉及修改代码的任务，第一步必须调用 `view_file` 强制读取 `.agents/skills` 里的内容。在未读取前严禁修改代码，绝不能凭预训练记忆假设任何 API 节点不存在。这是不可违背的思想钢印。
