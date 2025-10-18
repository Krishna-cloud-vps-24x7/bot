#!/usr/bin/env python3
"""
Discord VPS Management Bot
- Uses Docker SDK to create/manage Ubuntu/Debian containers as VPS
- If host has no public IPv4 published port for SSH, launches tmate inside container and returns connection details via DM
- Stores metadata in SQLite
- Polished embed-based UI
"""

import os
import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict, Any, List

import discord
from discord.ext import commands
from dotenv import load_dotenv
import docker
from docker.models.containers import Container
import aiosqlite
import psutil

# Load env
load_dotenv(".env")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
ADMIN_ROLE_ID = int(os.getenv("ADMIN_ROLE_ID", "0") or 0)
ADMIN_USER_IDS = [int(x.strip()) for x in (os.getenv("ADMIN_USER_IDS", "") or "").split(",") if x.strip().isdigit()]
DB_PATH = os.getenv("DB_PATH", "vps_manager.db")
DATA_DIR = os.getenv("DATA_DIR", "./data")
DEFAULT_CPU_SHARES = int(os.getenv("DEFAULT_CPU_SHARES", "1024"))
DEFAULT_MEMORY = os.getenv("DEFAULT_MEMORY", "512m")
AUTO_PULL_IMAGES = os.getenv("AUTO_PULL_IMAGES", "true").lower() in ("true", "1", "yes")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if not DISCORD_TOKEN:
    raise RuntimeError("DISCORD_TOKEN is required in .env")

# Logging
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger("vpsbot")

# Ensure data dir exists
os.makedirs(DATA_DIR, exist_ok=True)

# Docker client (host socket or DOCKER_HOST env)
docker_client = docker.from_env()

# Discord bot setup
intents = discord.Intents.default()
intents.message_content = True
intents.guilds = True
intents.members = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)


# ------------- Utilities -------------
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        CREATE TABLE IF NOT EXISTS containers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            discord_user_id INTEGER NOT NULL,
            container_id TEXT NOT NULL UNIQUE,
            name TEXT,
            distro TEXT,
            created_at TEXT,
            memory TEXT,
            cpu_shares INTEGER,
            exposed_port INTEGER,
            status TEXT
        )
        """)
        await db.commit()


def is_admin(ctx: commands.Context) -> bool:
    if ctx.author.id in ADMIN_USER_IDS:
        return True
    if ADMIN_ROLE_ID and ctx.guild:
        role = ctx.guild.get_role(ADMIN_ROLE_ID)
        if role and role in ctx.author.roles:
            return True
    return ctx.author.guild_permissions.administrator


def nice_embed(title: str, description: str = "", color: discord.Color = discord.Color.blurple()) -> discord.Embed:
    e = discord.Embed(title=title, description=description, color=color, timestamp=datetime.utcnow())
    e.set_footer(text="VPS Manager • secure container-based VPS")
    return e


async def save_container_metadata(discord_user_id: int, container: Container, distro: str, memory: str, cpu_shares: int, exposed_port: Optional[int]):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
        INSERT OR REPLACE INTO containers (discord_user_id, container_id, name, distro, created_at, memory, cpu_shares, exposed_port, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            discord_user_id,
            container.id,
            container.name,
            distro,
            datetime.utcnow().isoformat(),
            memory,
            cpu_shares,
            exposed_port,
            container.status
        ))
        await db.commit()


async def remove_container_metadata(container_id: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("DELETE FROM containers WHERE container_id = ?", (container_id,))
        await db.commit()


async def get_user_containers(user_id: int) -> List[Dict[str, Any]]:
    async with aiosqlite.connect(DB_PATH) as db:
        cur = await db.execute("SELECT * FROM containers WHERE discord_user_id = ?", (user_id,))
        rows = await cur.fetchall()
        keys = [x[0] for x in cur.description]
        return [dict(zip(keys, row)) for row in rows]


# ------------- Docker / tmate helpers -------------
async def pull_images():
    if not AUTO_PULL_IMAGES:
        return
    for img in ("ubuntu:22.04", "debian:12"):
        try:
            logger.info(f"Pulling {img}...")
            docker_client.images.pull(img)
            logger.info(f"Pulled {img}")
        except Exception as e:
            logger.warning(f"Could not pull {img}: {e}")


def build_container_name(user_id: int, distro: str) -> str:
    ts = datetime.utcnow().strftime("%y%m%d%H%M%S")
    return f"vps_{distro}_{user_id}_{ts}"


async def run_tmate_in_container(container: Container) -> Dict[str, Optional[str]]:
    """
    Install tmate (if missing) and run it to create a session.
    Returns dict with keys: 'ssh', 'web', 'raw'
    """
    result = {"ssh": None, "web": None, "raw": None}
    try:
        logger.info("Installing tmate in container %s", container.name)
        install_cmd = "apt-get update -qq && DEBIAN_FRONTEND=noninteractive apt-get install -y -qq tmate openssh-client || true"
        container.exec_run(cmd=["/bin/bash", "-lc", install_cmd], stdout=False, stderr=False, demux=False)
        run_cmd = "/usr/bin/tmate -S /tmp/tmate.sock new-session -d; sleep 1; /usr/bin/tmate -S /tmp/tmate.sock display -p '#{tmate_ssh} #{tmate_web}'"
        exec_res = container.exec_run(cmd=["/bin/bash", "-lc", run_cmd], stdout=True, stderr=True, demux=True)
        out, err = exec_res.output or (b"", b"")
        raw = (out + (err or b"")).decode(errors="ignore").strip()
        result["raw"] = raw
        parts = raw.split()
        if len(parts) >= 1:
            result["ssh"] = parts[0]
        if len(parts) >= 2:
            result["web"] = parts[1]
        logger.info("tmate result: %s", result)
    except Exception as e:
        logger.exception("tmate creation failed: %s", e)
    return result


# ------------- Bot events & commands -------------
def require_admin():
    async def predicate(ctx):
        if is_admin(ctx):
            return True
        await ctx.reply(embed=nice_embed("Permission denied", "You don't have permission to run this command.", discord.Color.red()))
        return False
    return commands.check(predicate)


@bot.event
async def on_ready():
    logger.info(f"Logged in as {bot.user} ({bot.user.id})")
    await init_db()
    await pull_images()
    await bot.change_presence(activity=discord.Game("Managing containers ⚙️"))
    logger.info("Bot ready.")


@bot.command(name="create", aliases=["new", "vps-create"])
async def cmd_create(ctx, distro: str = "ubuntu", ram: str = DEFAULT_MEMORY, cpu_shares: int = DEFAULT_CPU_SHARES):
    """Create a new VPS container. Usage: !create ubuntu|debian [ram] [cpu_shares]"""
    distro = distro.lower()
    if distro not in ("ubuntu", "debian"):
        await ctx.reply(embed=nice_embed("Unsupported distro ❌", "Supported distros: ubuntu, debian", discord.Color.red()))
        return

    author = ctx.author
    name = build_container_name(author.id, distro)
    embed = nice_embed("Creating VPS 🚀", f"Preparing a new {distro} container for {author.mention}\nMemory: {ram} • CPU shares: {cpu_shares}", discord.Color.green())
    msg = await ctx.reply(embed=embed)

    try:
        image = "ubuntu:22.04" if distro == "ubuntu" else "debian:12"
        container = docker_client.containers.run(
            image=image,
            name=name,
            command="/bin/bash -lc 'while true; do sleep 3600; done'",
            detach=True,
            tty=True,
            stdin_open=True,
            cpu_shares=cpu_shares,
            mem_limit=ram,
            labels={"vpsbot_owner": str(author.id), "vpsbot_distro": distro},
            hostname=name,
        )

        # Setup: install openssh-server and create vpsuser with a random password - note: in production generate per-container SSH keys
        setup_script = """
        set -e
        apt-get update -qq
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server sudo
        mkdir -p /var/run/sshd
        id -u vpsuser >/dev/null 2>&1 || useradd -m -s /bin/bash vpsuser
        echo 'vpsuser:VpsPassw0rd!' | chpasswd
        usermod -aG sudo vpsuser || true
        sed -i 's/^#PasswordAuthentication yes/PasswordAuthentication yes/' /etc/ssh/sshd_config || true
        service ssh restart || /etc/init.d/ssh restart || true
        """
        container.exec_run(cmd=["/bin/bash", "-lc", setup_script], stdout=False, stderr=False, demux=False)
        await save_container_metadata(author.id, container, distro, ram, cpu_shares, None)

        dm_embed = nice_embed("VPS Created ✅", f"Your {distro} VPS is ready.\n**Container**: `{container.name}`\n**User**: `vpsuser`", discord.Color.green())
        dm_embed.add_field(name="Password", value="`VpsPassw0rd!` (change immediately!)", inline=False)
        dm_embed.add_field(name="Connect", value=f"Use `!ssh {container.name}` to get connection details.", inline=False)
        try:
            await author.send(embed=dm_embed)
        except Exception:
            await ctx.reply(embed=nice_embed("Couldn't DM you ❗", "I couldn't DM you — please enable DMs from this server.", discord.Color.orange()))
        await msg.edit(embed=nice_embed("Created ✅", f"Container `{container.name}` created for {author.mention}. Check your DMs.", discord.Color.green()))
    except Exception as e:
        logger.exception("Error creating container: %s", e)
        await msg.edit(embed=nice_embed("Creation failed ❌", f"Error: {e}", discord.Color.red()))


@bot.command(name="list", aliases=["vps-list", "myvps"])
async def cmd_list(ctx):
    """List your VPS containers"""
    items = await get_user_containers(ctx.author.id)
    if not items:
        await ctx.reply(embed=nice_embed("No VPS found", "You don't have any VPS containers.", discord.Color.orange()))
        return
    e = nice_embed("Your VPS containers 📦", f"Found {len(items)} containers for {ctx.author.mention}", discord.Color.blue())
    for it in items:
        e.add_field(name=f"{it['name']}", value=f"ID: `{it['container_id'][:12]}`\nDistro: {it['distro']}\nStatus: {it['status']}\nMemory: {it['memory']}", inline=False)
    await ctx.reply(embed=e)


@bot.command(name="info", aliases=["vps-info"])
async def cmd_info(ctx, container_name: str):
    """Show info about a container: !info <container_name>"""
    try:
        container = docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        await ctx.reply(embed=nice_embed("Not found ❌", f"Container `{container_name}` not found.", discord.Color.red()))
        return
    details = container.attrs
    ss = nice_embed(f"Info: {container.name} 🔎", "", discord.Color.teal())
    ss.add_field(name="ID", value=container.id, inline=False)
    ss.add_field(name="Image", value=details.get("Config", {}).get("Image", "unknown"), inline=True)
    ss.add_field(name="Status", value=container.status, inline=True)
    nets = details.get("NetworkSettings", {})
    ip = nets.get("IPAddress") or "N/A"
    ss.add_field(name="Container IP", value=ip, inline=True)
    ports = nets.get("Ports", {}) or {}
    ss.add_field(name="Published ports", value=str(ports), inline=False)
    await ctx.reply(embed=ss)


@bot.command(name="start")
async def cmd_start(ctx, container_name: str):
    """Start a stopped container"""
    try:
        container = docker_client.containers.get(container_name)
        container.start()
        await ctx.reply(embed=nice_embed("Started ▶️", f"Container `{container.name}` started.", discord.Color.green()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="stop")
async def cmd_stop(ctx, container_name: str):
    """Stop a running container"""
    try:
        container = docker_client.containers.get(container_name)
        container.stop(timeout=10)
        await ctx.reply(embed=nice_embed("Stopped ⏹️", f"Container `{container.name}` stopped.", discord.Color.orange()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="restart")
async def cmd_restart(ctx, container_name: str):
    """Restart container"""
    try:
        container = docker_client.containers.get(container_name)
        container.restart()
        await ctx.reply(embed=nice_embed("Restarted 🔁", f"Container `{container.name}` restarted.", discord.Color.green()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="destroy", aliases=["rm", "delete"])
async def cmd_destroy(ctx, container_name: str):
    """Destroy (remove) container permanently"""
    try:
        container = docker_client.containers.get(container_name)
        owner = container.labels.get("vpsbot_owner")
        if owner and int(owner) != ctx.author.id and not is_admin(ctx):
            await ctx.reply(embed=nice_embed("Permission denied ❌", "You are not allowed to remove this container.", discord.Color.red()))
            return
        container.stop(timeout=5)
        container.remove(force=True)
        await remove_container_metadata(container.id)
        await ctx.reply(embed=nice_embed("Removed 🗑️", f"Container `{container_name}` removed.", discord.Color.red()))
    except docker.errors.NotFound:
        await ctx.reply(embed=nice_embed("Not found ❌", f"Container `{container_name}` not found.", discord.Color.red()))
    except Exception as e:
        logger.exception("Error removing container: %s", e)
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="exec")
async def cmd_exec(ctx, container_name: str, *, command: str):
    """Execute a command inside container (owner/admin)"""
    try:
        container = docker_client.containers.get(container_name)
        owner = container.labels.get("vpsbot_owner")
        if owner and int(owner) != ctx.author.id and not is_admin(ctx):
            await ctx.reply(embed=nice_embed("Permission denied ❌", "You are not allowed to exec in this container.", discord.Color.red()))
            return
        exec_res = container.exec_run(cmd=["/bin/bash", "-lc", command], stdout=True, stderr=True, demux=True)
        out, err = exec_res.output or (b"", b"")
        out_s = out.decode(errors="ignore") if out else ""
        err_s = err.decode(errors="ignore") if err else ""
        embed = nice_embed(f"Exec output ⚙️ {container.name}", f"Command: `{command}`", discord.Color.blue())
        embed.add_field(name="Stdout", value=f"```\n{(out_s[:1900] or '(none)')}\n```", inline=False)
        if err_s:
            embed.add_field(name="Stderr", value=f"```\n{(err_s[:1900])}\n```", inline=False)
        await ctx.reply(embed=embed)
    except docker.errors.NotFound:
        await ctx.reply(embed=nice_embed("Not found ❌", "Container not found.", discord.Color.red()))
    except Exception as e:
        logger.exception("Exec error: %s", e)
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="ssh")
async def cmd_ssh(ctx, container_name: str):
    """
    Provide SSH details.
    If direct SSH is not available, create a tmate session and DM the user the connection strings.
    """
    try:
        container = docker_client.containers.get(container_name)
    except docker.errors.NotFound:
        await ctx.reply(embed=nice_embed("Not found ❌", f"Container `{container_name}` not found.", discord.Color.red()))
        return

    owner = container.labels.get("vpsbot_owner")
    if owner and int(owner) != ctx.author.id and not is_admin(ctx):
        await ctx.reply(embed=nice_embed("Permission denied ❌", "You cannot see connection details for this container.", discord.Color.red()))
        return

    ports = container.attrs.get("NetworkSettings", {}).get("Ports", {}) or {}
    ssh_host_port = None
    ssh_found = False
    for k, v in ports.items():
        try:
            if k.startswith("22/"):
                if v:
                    binding = v[0]
                    ssh_host = binding.get("HostIp", "")
                    ssh_port = binding.get("HostPort", "")
                    ssh_host_port = (ssh_host, ssh_port)
                    ssh_found = True
                    break
        except Exception:
            continue

    if ssh_found and ssh_host_port and ssh_host_port[0] not in ("0.0.0.0", "", None):
        host, port = ssh_host_port
        embed = nice_embed("SSH details 🔐", f"Direct SSH is available for `{container.name}`", discord.Color.green())
        embed.add_field(name="SSH", value=f"`ssh vpsuser@{host} -p {port}`\nPassword: `VpsPassw0rd!`", inline=False)
        try:
            await ctx.author.send(embed=embed)
            await ctx.reply(embed=nice_embed("Sent via DM ✉️", "I sent SSH connection details to your DMs.", discord.Color.green()))
        except Exception:
            await ctx.reply(embed=nice_embed("Couldn't DM you ❗", "I couldn't DM you — please enable DMs from this server.", discord.Color.orange()))
        return

    await ctx.reply(embed=nice_embed("No public SSH found — creating tmate session 🔁", "I will create a tmate session inside the container and DM you the connection.", discord.Color.orange()))
    tmate_info = await run_tmate_in_container(container)
    if tmate_info.get("ssh") or tmate_info.get("web"):
        dm = nice_embed("tmate session ready 🔗", f"Container: `{container.name}`", discord.Color.green())
        if tmate_info.get("ssh"):
            dm.add_field(name="SSH", value=f"`{tmate_info['ssh']}`", inline=False)
        if tmate_info.get("web"):
            dm.add_field(name="Web (browser)", value=f"{tmate_info['web']}", inline=False)
        dm.add_field(name="Note", value="tmate sessions are temporary. Use to set up a persistent SSH if needed.", inline=False)
        try:
            await ctx.author.send(embed=dm)
            await ctx.reply(embed=nice_embed("tmate ready ✅", "tmate connection details sent to your DMs.", discord.Color.green()))
        except Exception:
            await ctx.reply(embed=nice_embed("Couldn't DM you ❗", "I couldn't DM you — please enable DMs from this server.", discord.Color.orange()))
    else:
        await ctx.reply(embed=nice_embed("Failed ❌", "Could not create tmate session. Admins have been notified.", discord.Color.red()))
        for admin_id in ADMIN_USER_IDS:
            try:
                admin = await bot.fetch_user(admin_id)
                await admin.send(embed=nice_embed("tmate creation failed", f"Failed to create tmate for {container.name}", discord.Color.red()))
            except Exception:
                pass


@bot.command(name="setlimit")
async def cmd_setlimit(ctx, container_name: str, ram: Optional[str] = None, cpu_shares: Optional[int] = None):
    """Set new resource limits (stop/start required for some changes). Usage: !setlimit <container> [ram] [cpu_shares]"""
    try:
        container = docker_client.containers.get(container_name)
        owner = container.labels.get("vpsbot_owner")
        if owner and int(owner) != ctx.author.id and not is_admin(ctx):
            await ctx.reply(embed=nice_embed("Permission denied ❌", "You are not allowed to modify this container.", discord.Color.red()))
            return
        changed = []
        if cpu_shares:
            container.update(cpu_shares=cpu_shares)
            changed.append(f"cpu_shares={cpu_shares}")
        if ram:
            container.update(mem_limit=ram)
            changed.append(f"memory={ram}")
        await ctx.reply(embed=nice_embed("Limits updated ⚙️", "Updated: " + ", ".join(changed), discord.Color.green()))
    except Exception as e:
        logger.exception("Setlimit error: %s", e)
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="logs")
async def cmd_logs(ctx, container_name: str, tail: int = 200):
    """Get logs from a container"""
    try:
        container = docker_client.containers.get(container_name)
        logs = container.logs(tail=tail).decode(errors="ignore")
        embed = nice_embed(f"Logs 📜 {container.name}", f"Last {tail} lines", discord.Color.dark_blue())
        embed.add_field(name="Logs", value=f"```\n{logs[:1900]}\n```")
        await ctx.reply(embed=embed)
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="snapshot", aliases=["commit"])
async def cmd_snapshot(ctx, container_name: str, snapshot_name: Optional[str] = None):
    """Create a snapshot (docker commit) of the container"""
    try:
        container = docker_client.containers.get(container_name)
        owner = container.labels.get("vpsbot_owner")
        if owner and int(owner) != ctx.author.id and not is_admin(ctx):
            await ctx.reply(embed=nice_embed("Permission denied ❌", "You cannot snapshot this container.", discord.Color.red()))
            return
        snapshot_name = snapshot_name or f"{container.name}_snapshot_{datetime.utcnow().strftime('%y%m%d%H%M')}"
        image = container.commit(repository=snapshot_name)
        await ctx.reply(embed=nice_embed("Snapshot created 📸", f"Image: `{snapshot_name}` (id: {image.id[:12]})", discord.Color.green()))
    except Exception as e:
        logger.exception("Snapshot error: %s", e)
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


# Admin commands
@bot.command(name="force-stop")
@require_admin()
async def cmd_force_stop(ctx, container_name: str):
    """Admin: force stop container"""
    try:
        c = docker_client.containers.get(container_name)
        c.kill()
        await ctx.reply(embed=nice_embed("Force stopped 🛑", f"Container `{container_name}` killed.", discord.Color.red()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="user-info")
@require_admin()
async def cmd_user_info(ctx, discord_user_id: int):
    """Admin: get user's containers and info"""
    items = await get_user_containers(discord_user_id)
    embed = nice_embed("User Info 🔎", f"User ID: `{discord_user_id}` — {len(items)} containers", discord.Color.blue())
    for it in items:
        embed.add_field(name=it['name'], value=f"ContainerID: `{it['container_id'][:12]}`\nDistro: {it['distro']}\nStatus: {it['status']}", inline=False)
    await ctx.reply(embed=embed)


@bot.command(name="announce")
@require_admin()
async def cmd_announce(ctx, *, message: str):
    """Admin: announce a message to channel"""
    await ctx.send(embed=nice_embed("Announcement 📢", message, discord.Color.gold()))


@bot.command(name="help", aliases=["commands"])
async def cmd_help(ctx):
    """Show help and commands list"""
    e = discord.Embed(title="VPS Manager - Help 🧭", color=discord.Color.blurple())
    e.set_author(name="VPS Bot", icon_url=bot.user.avatar.url if bot.user.avatar else None)
    e.add_field(name="Basic (create/manage)", value="`!create <ubuntu|debian> [ram] [cpu_shares]` • `!list` • `!info <name>`", inline=False)
    e.add_field(name="Connection", value="`!ssh <name>` • `!exec <name> <cmd>` • `!logs <name>`", inline=False)
    e.add_field(name="Control", value="`!start` • `!stop` • `!restart` • `!destroy`", inline=False)
    e.add_field(name="Admin", value="`!user-info <id>` • `!force-stop <name>` • `!announce <message>`", inline=False)
    e.set_footer(text="Use commands responsibly. Many commands DM the container owner.")
    await ctx.reply(embed=e)


@bot.command(name="host-info")
@require_admin()
async def cmd_host_info(ctx):
    """Show host system stats"""
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disks = psutil.disk_usage('/')
    embed = nice_embed("Host Info 🖥️", "", discord.Color.dark_teal())
    embed.add_field(name="CPU %", value=f"{cpu}%", inline=True)
    embed.add_field(name="Memory", value=f"{mem.percent}% ({round(mem.total/1024**3,2)}GB)", inline=True)
    embed.add_field(name="Disk", value=f"{disks.percent}% ({round(disks.total/1024**3,2)}GB)", inline=True)
    try:
        info = docker_client.info()
        embed.add_field(name="Docker containers", value=f"{info.get('Containers', 'N/A')}", inline=True)
        embed.add_field(name="Docker images", value=f"{info.get('Images', 'N/A')}", inline=True)
    except Exception:
        pass
    await ctx.reply(embed=embed)


@bot.command(name="list-images")
@require_admin()
async def cmd_list_images(ctx):
    """List docker images available"""
    imgs = docker_client.images.list()
    embed = nice_embed("Docker Images 🖼️", f"Found {len(imgs)} images", discord.Color.blue())
    for i in imgs[:20]:
        tags = ", ".join(i.tags) if i.tags else i.short_id
        embed.add_field(name=i.short_id, value=tags, inline=False)
    await ctx.reply(embed=embed)


@bot.command(name="pull-image")
@require_admin()
async def cmd_pull_image(ctx, image: str):
    """Pull docker image on host"""
    await ctx.reply(embed=nice_embed("Pulling image ⬇️", f"Pulling `{image}` — this may take a while.", discord.Color.orange()))
    try:
        docker_client.images.pull(image)
        await ctx.reply(embed=nice_embed("Pulled ✅", f"Image `{image}` pulled.", discord.Color.green()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Failed ❌", str(e), discord.Color.red()))


@bot.command(name="owner")
async def cmd_owner(ctx, container_name: str):
    """Return the Discord ID who owns the container"""
    try:
        container = docker_client.containers.get(container_name)
        owner = container.labels.get("vpsbot_owner")
        await ctx.reply(embed=nice_embed("Owner 👤", f"Owner Discord ID: `{owner}`", discord.Color.blue()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="rename")
async def cmd_rename(ctx, container_name: str, new_name: str):
    """Rename container (owner/admin)"""
    try:
        c = docker_client.containers.get(container_name)
        owner = c.labels.get("vpsbot_owner")
        if owner and int(owner) != ctx.author.id and not is_admin(ctx):
            await ctx.reply(embed=nice_embed("Permission denied ❌", "You cannot rename this container.", discord.Color.red()))
            return
        c.rename(new_name)
        await ctx.reply(embed=nice_embed("Renamed ✏️", f"Renamed to `{new_name}`", discord.Color.green()))
    except Exception as e:
        await ctx.reply(embed=nice_embed("Error ❌", str(e), discord.Color.red()))


@bot.command(name="whitelist-add")
@require_admin()
async def cmd_whitelist_add(ctx, discord_user_id: int):
    """Admin: add a user to admin list (in-memory only - update .env manually to persist)"""
    if discord_user_id not in ADMIN_USER_IDS:
        ADMIN_USER_IDS.append(discord_user_id)
    await ctx.reply(embed=nice_embed("Whitelist ✅", f"User `{discord_user_id}` added to admin list (in-memory). Update .env to persist.", discord.Color.green()))


@bot.command(name="ping")
async def cmd_ping(ctx):
    """Latency check"""
    d = bot.latency * 1000
    await ctx.reply(embed=nice_embed("Pong 🏓", f"Latency: {d:.0f} ms", discord.Color.blurple()))


@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        return
    logger.exception("Command error: %s", error)
    try:
        await ctx.reply(embed=nice_embed("Error ❌", str(error), discord.Color.red()))
    except Exception:
        pass


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
