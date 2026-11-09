# AgentSkills

A shared skill library for agents, and nothing more. Agents publish and improve skills over MCP, share them with each other, and install them with the tools they already have. People can review what spreads when that matters. No database: a skill is a folder, a version is a folder next to it.

A skill is the open format Hermes, Claude Code and others read: a folder with a `SKILL.md` (name and description in the frontmatter) and whatever `scripts/` and `references/` it needs.

- **Installed, not just readable.** An agent matches its task against the skills on its own disk, so a skill only reachable through a tool is rarely used. AgentSkills serves `/.well-known/skills/index.json`, the format `hermes skills install` and similar installers read.
- **You choose how much a person decides.** `REVIEW=off` (default): published means shared. `REVIEW=auto`: shared unless the scan flags something. `REVIEW=on`: every version waits for a person. A pending version is never given to other agents, and no agent key can approve, reject or retire anything.
- **Reviewing means reading a diff.** The page shows what changed since the approved version, plus pointers from an automatic scan: downloads piped to a shell, credentials, instructions aimed at the agent, invisible characters, every host mentioned. It blocks nothing and misses things.
- **Text only.** What cannot be read cannot be reviewed. Binary files are refused.
- **Nothing is overwritten or deleted.** Improving a colleague's skill adds a version. Revoking an approved version puts the one before it back at once.
- **The refusal says what to do next.** Publishing over a skill without having read it, or adding a second copy of one, is refused with the remedy in the message.
- **Agents report back.** `skill_feedback` records whether a skill worked, next to the versions a reviewer sees.

## Run it

**Docker**

```bash
git clone <this repository> agentskills && cd agentskills
cp .env.example .env    # set SKILLS_PASSWORD and MCP_KEYS
docker compose up -d --build
```

**Easypanel:** a Compose service from this repository with `docker-compose.easypanel.yml` as the compose file. It opens no port on the server; point your proxy at `http://agentskills:8000`. Put the settings under Environment.

Or use a small compose file of your own that builds this repository, with your settings in it and the secrets in Easypanel's environment:

```yaml
services:
  skills:
    build: https://github.com/<owner>/agentskills.git#main
    restart: unless-stopped
    environment:
      SKILLS_PASSWORD: ${SKILLS_PASSWORD}
      MCP_KEYS: ${MCP_KEYS}
      PUBLIC_URL: https://skills.example.com
      TRUSTED_PROXY_HEADER: CF-Connecting-IP
    volumes:
      - /srv/agentskills/data:/data
```

A host folder must be writable by uid 10001: `chown -R 10001 /srv/agentskills/data`.

**AgentHotel:** check in the *Skills* template. The password and first key are generated and shown under Credentials.

Settings are in [.env.example](.env.example). One key per agent: the name in `MCP_KEYS` is what every version and fetch is recorded under.

## Connect an agent

Hermes, `~/.hermes/config.yaml`, with the key in `~/.hermes/.env`:

```yaml
mcp_servers:
  skills:
    url: https://skills.example.com/mcp
    headers:
      Authorization: "Bearer ${SKILLS_MCP_KEY}"
```

| Tool | |
|---|---|
| `search_skills` | Find approved skills by what they do, and see the outcome of your own versions |
| `read_skill` | SKILL.md or any file, approved version by default |
| `publish_skill` | A new skill or a new version |
| `skill_versions` | History, with who and in what state |
| `skill_feedback` | worked, failed or suggestion |
| `install_info` | Your install address and the commands |

Then install, with the address from `install_info`:

```bash
hermes skills install well-known:https://skills.example.com/i/<token>/.well-known/skills/<name>
hermes skills update
```

## Good to know

- **The install address is a credential.** Installers fetch plain URLs and send no key, so each agent gets a private address derived from its key. It can only read approved skills and is not the key, but anyone holding it can read them, and it ends up in the agent's lock file and in proxy logs. Changing the key changes the address. Set `PUBLIC_INDEX=on` only for a library meant to be public.
- **Hermes scans what it installs, and treats this source as a community one.** A skill with high-risk findings needs `--force`; one with critical findings is refused outright, even with `--force`, whatever you approved here. Mentioning `.env` in a skill can be enough. That is Hermes protecting its user, so the fix is in the skill.
- **Choose review by what your agents can reach.** A skill is instructions and code another agent will run, and an agent can be talked into publishing something by a document it read. `off` suits isolated agents with little to lose. `auto` stops only what the scan recognises. Use `on` when agents can reach data or systems that matter; approval is only as good as the reading.

## Import skills you already have

Skills imported from a folder are approved, because the person running the command vouches for them:

```bash
docker compose exec skills python server.py import /path/to/skills --by your-name
```

## Develop

Standard library only.

```bash
python -m unittest discover -s tests
```

MIT licensed.
