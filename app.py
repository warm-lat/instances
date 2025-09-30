import hmac
import hashlib
import logging
import uvicorn
import asyncio
import shutil
import docker
import os
import subprocess
import json

from fastapi import FastAPI, HTTPException, Header, Request, Depends
from fastapi.responses import JSONResponse

from git import Repo
from pathlib import Path
from datetime import datetime
from typing import Dict
from dotenv import load_dotenv
load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/var/log/evict-manager.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

app = FastAPI()

class SecurityManager:
    def __init__(self, api_key: str):
        self.api_key = "t76oev5UkeMyo8XQwv5Ozwo3amVsi"
        self.logger = logging.getLogger(__name__)
        
    def verify_signature(self, data: Dict, timestamp: str, signature: str, api_key: str) -> bool:
        if not hmac.compare_digest(api_key, self.api_key):
            self.logger.warning("API key mismatch")
            return False
            
        try:
            ts = int(timestamp)
            if abs(datetime.now().timestamp() - ts) > 300:
                self.logger.warning(f"Timestamp too old: {timestamp}")
                return False
        except ValueError:
            self.logger.error(f"Invalid timestamp format: {timestamp}")
            return False
            
        message = f"{timestamp}:{json.dumps(data, sort_keys=True)}"
        expected = hmac.new(
            self.api_key.encode(),
            message.encode(),
            hashlib.sha256
        ).hexdigest()
        
        if not hmac.compare_digest(signature, expected):
            self.logger.warning("Signature mismatch")
            return False
            
        return True

class InstanceManager:
    def __init__(self):
        self.base_path = Path("/root/instances")
        self.repo_url = "git@github.com:EvictServices/evict.git"
        self.branch = "evict-instance"
        self.docker_client = docker.from_env()
        self.instances: Dict[int, Dict] = {}
        self.logger = logging.getLogger(__name__)
        
        self.base_path.mkdir(parents=True, exist_ok=True)
        
        try:
            playwright_path = subprocess.check_output(
                "which playwright",
                shell=True,
                env={"PATH": "/root/evict-test/venv/bin:" + os.environ["PATH"]}
            ).decode().strip()
            self.logger.info(f"Found playwright at: {playwright_path}")
        except subprocess.CalledProcessError:
            self.logger.error("Playwright not found in PATH!")
            
        try:
            pm2_path = subprocess.check_output("which pm2", shell=True).decode().strip()
            self.logger.info(f"Found pm2 at: {pm2_path}")
        except subprocess.CalledProcessError:
            self.logger.error("PM2 not found in PATH!")
            
        self.logger.info(f"Current PATH: {os.environ.get('PATH', 'Not set')}")
        self.logger.info(f"Initialized InstanceManager with base path: {self.base_path}")
        
    async def run_command(self, cmd: str, cwd: str = None, env: dict = None) -> str:
        self.logger.info(f"Running command: {cmd}")
        self.logger.debug(f"Working directory: {cwd}")
        
        command_env = os.environ.copy()
        if env:
            command_env.update(env)
        
        venv_path = "/root/evict-test/venv/bin"
        if 'PATH' in command_env:
            command_env['PATH'] = f"{venv_path}:{command_env['PATH']}"
        else:
            command_env['PATH'] = f"{venv_path}:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        
        self.logger.debug(f"Environment: {command_env}")
        
        process = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=command_env
        )
        
        stdout, stderr = await process.communicate()
        
        if process.returncode != 0:
            stdout_text = stdout.decode() if stdout else ""
            stderr_text = stderr.decode() if stderr else ""
            error_message = f"Command failed with return code {process.returncode}\nSTDOUT:\n{stdout_text}\nSTDERR:\n{stderr_text}"
            self.logger.error(f"Command failed: {cmd}")
            self.logger.error(f"Error details: {error_message}")
            raise Exception(error_message)
            
        self.logger.debug(f"Command output: {stdout.decode().strip()}")
        return stdout.decode().strip()

    async def cleanup_existing(self, bot_name: str) -> None:
        self.logger.info(f"Cleaning up existing instance: {bot_name}")
        
        try:
            try:
                await self.run_command(f"pm2 delete {bot_name}")
                self.logger.info(f"Stopped PM2 process for {bot_name}")
            except Exception as e:
                self.logger.debug(f"No PM2 process found for {bot_name}: {e}")

            try:
                await self.run_command(
                    f"docker exec -i setup-postgres-1 psql -U admin -c 'DROP DATABASE IF EXISTS {bot_name};'"
                )
                self.logger.info(f"Dropped database {bot_name}")
            except Exception as e:
                self.logger.error(f"Error dropping database: {e}")

            instance_path = self.base_path / bot_name
            if instance_path.exists():
                shutil.rmtree(instance_path)
                self.logger.info(f"Removed directory {instance_path}")
                
        except Exception as e:
            self.logger.error(f"Error during cleanup: {e}")
            raise

    async def setup_instance(self, bot_name: str, token: str, owner: dict, prefix: str = ";") -> bool:
        self.logger.info(f"Setting up new instance: {bot_name}")
        
        try:
            await self.cleanup_existing(bot_name)
            
            instance_path = self.base_path / bot_name
            instance_path.mkdir(parents=True, exist_ok=True)
            self.logger.info(f"Created instance directory: {instance_path}")

            self.logger.info("Cloning repository")
            Repo.clone_from(
                self.repo_url,
                instance_path,
                branch=self.branch,
                depth=1
            )

            self.logger.info("Creating .env file")
            env_content = f"""# Discord Bot Configuration
                            DISCORD_TOKEN={token}
                            DISCORD_CLIENT_ID=your_client_id_here
                            DISCORD_CLIENT_SECRET=your_client_secret_here

                            # Bot Settings
                            BOT_PREFIX={prefix}

                            # Database
                            DATABASE_DSN=postgres://admin:0J9RLTUDWMRl3IrdAE3as8@localhost:5433/{bot_name}

                            # Instance Owner
                            INSTANCE_OWNER_ID={owner.get('id')}
                            INSTANCE_OWNER_USERNAME={owner.get('username')}
                            INSTANCE_OWNER_EMAIL={owner.get('email')}
                            """
            env_file = instance_path / ".env"
            env_file.write_text(env_content)

            self.logger.info("Setting up virtual environment")
            await self.run_command("python3 -m venv venv", cwd=str(instance_path))
            venv_python = instance_path / "venv/bin/python"
            venv_pip = instance_path / "venv/bin/pip"

            self.logger.info("Installing requirements")
            await self.run_command(f"{venv_pip} install -r requirements.txt", cwd=str(instance_path))
            
            self.logger.info("Installing additional packages")
            additional_packages = ["matplotlib", "posthog", "scipy", "plotly", "stripe"]
            await self.run_command(f"{venv_pip} install {' '.join(additional_packages)}", cwd=str(instance_path))

            self.logger.info("Creating cookies.txt")
            cookies_content = "# Netscape HTTP Cookie File\n# This is an empty cookie file, created for compatibility."
            cookies_file = instance_path / "cookies.txt"
            cookies_file.write_text(cookies_content)

            try:
                df_output = await self.run_command("df -h /")
                self.logger.info(f"Disk space status:\n{df_output}")
            except Exception as e:
                self.logger.warning(f"Could not check disk space: {e}")

            try:
                pg_space = await self.run_command(
                    "docker exec -i setup-postgres-1 df -h /var/lib/postgresql/data"
                )
                self.logger.info(f"PostgreSQL disk space:\n{pg_space}")
            except Exception as e:
                self.logger.warning(f"Could not check PostgreSQL space: {e}")

            self.logger.info(f"Creating database: {bot_name}")
            try:
                await self.run_command(
                    f"docker exec -i setup-postgres-1 psql -U admin -c 'CREATE DATABASE {bot_name};'"
                )

                self.logger.info("Verifying database creation")
                verify_cmd = f"docker exec -i setup-postgres-1 psql -U admin -c \"SELECT datname FROM pg_database WHERE lower(datname) = lower('{bot_name}');\""
                db_exists = await self.run_command(verify_cmd)
                
                if "datname" not in db_exists:
                    raise Exception(f"Database {bot_name} was not created successfully")

                await asyncio.sleep(1)

                self.logger.info("Applying database schema")
                schema_cmd = f"cat /root/setup/latest.sql | docker exec -i setup-postgres-1 psql -U admin -d {bot_name.lower()}"
                self.logger.info(f"Running schema command: {schema_cmd}")
                await self.run_command(schema_cmd)
            except Exception as e:
                self.logger.error(f"Database operation failed: {e}")
                try:
                    db_exists = await self.run_command(
                        f"docker exec -i setup-postgres-1 psql -U admin -lqt | cut -d \| -f 1 | grep -w {bot_name}"
                    )
                    if db_exists:
                        self.logger.error(f"Database {bot_name} already exists")
                    else:
                        pg_status = await self.run_command(
                            "docker exec -i setup-postgres-1 pg_isready"
                        )
                        self.logger.error(f"PostgreSQL status: {pg_status}")
                except Exception as check_e:
                    self.logger.error(f"Could not check database status: {check_e}")
                raise

            await asyncio.sleep(1) 

            self.logger.info("Installing Playwright dependencies")
            try:
                playwright_cmd = "/root/evict-test/venv/bin/playwright"
                await self.run_command(f"sudo {playwright_cmd} install-deps")
                await self.run_command(f"{playwright_cmd} install")
            except Exception as e:
                self.logger.warning(f"Playwright installation failed (non-critical): {e}")

            self.logger.info("Reinstalling discord.py")
            try:
                await self.run_command(f"{venv_pip} uninstall -y discord.py", cwd=str(instance_path))
                await self.run_command(
                    f"{venv_pip} install git+https://github.com/parelite/discord.py --force-reinstall",
                    cwd=str(instance_path)
                )
            except Exception as e:
                self.logger.error(f"Discord.py installation failed: {e}")
                raise

            self.logger.info("Starting bot with PM2")
            pm2_command = f"pm2 start {venv_python} --name {bot_name} -- main.py"
            await self.run_command(pm2_command, cwd=str(instance_path))

            self.instances[bot_name] = {
                "path": str(instance_path),
                "status": "active",
                "created_at": datetime.now().isoformat()
            }
            
            self.logger.info(f"Successfully set up instance: {bot_name}")
            return True
            
        except Exception as e:
            error_details = f"Failed to setup instance {bot_name}:\n"
            error_details += f"Error type: {type(e).__name__}\n"
            error_details += f"Error message: {str(e)}\n"
            
            try:
                disk_space = await self.run_command("df -h /")
                error_details += f"\nDisk space status:\n{disk_space}\n"
            except Exception as space_e:
                error_details += f"\nCould not check disk space: {space_e}\n"
            
            try:
                memory_status = await self.run_command("free -h")
                error_details += f"\nMemory status:\n{memory_status}\n"
            except Exception as mem_e:
                error_details += f"\nCould not check memory: {mem_e}\n"

            self.logger.error(error_details)
            await self.cleanup_instance(bot_name)
            return False

    async def cleanup_instance(self, bot_name: str) -> None:
        self.logger.info(f"Cleaning up instance: {bot_name}")
        await self.cleanup_existing(bot_name)

security_manager = SecurityManager(os.getenv("API_KEY"))
instance_manager = InstanceManager()

async def verify_request(
    request: Request,
    x_timestamp: str = Header(...),
    x_signature: str = Header(...),
    x_api_key: str = Header(...)
) -> Dict:
    logger.info("Verifying incoming request")
    data = await request.json()
    if not security_manager.verify_signature(data, x_timestamp, x_signature, x_api_key):
        logger.warning("Invalid signature or API key")
        raise HTTPException(status_code=401, detail="Invalid signature or API key")
    return data

@app.post("/deploy")
async def deploy_instance(data: Dict = Depends(verify_request)):
    logger.info("Received deploy request")
    bot_name = data.get("bot_name")
    token = data.get("token")
    owner = data.get("owner", {})
    prefix = data.get("prefix", ";") 
    
    if not bot_name or not token:
        logger.warning("Missing required fields")
        raise HTTPException(status_code=400, detail="Missing bot_name or token field")
        
    try:
        success = await instance_manager.setup_instance(bot_name, token, owner, prefix)
        if not success:
            logger.error("Failed to deploy instance")
            raise HTTPException(status_code=500, detail="Failed to deploy instance")
            
        logger.info(f"Successfully deployed instance: {bot_name}")
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "message": f"Instance deployed for bot {bot_name}"
            }
        )
        
    except Exception as e:
        logger.error(f"Deployment failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.delete("/instance/{bot_name}")
async def delete_instance(
    bot_name: str,
    request: Request,
    x_timestamp: str = Header(...),
    x_signature: str = Header(...),
    x_api_key: str = Header(...)
):
    logger.info(f"Received delete request for {bot_name}")
    data = {"bot_name": bot_name}
    if not security_manager.verify_signature(data, x_timestamp, x_signature, x_api_key):
        logger.warning("Invalid signature or API key")
        raise HTTPException(status_code=401, detail="Invalid signature or API key")
        
    try:
        await instance_manager.cleanup_instance(bot_name)
        logger.info(f"Successfully deleted instance: {bot_name}")
        return JSONResponse(
            status_code=200,
            content={"status": "success", "message": f"Instance {bot_name} deleted"}
        )
    except Exception as e:
        logger.error(f"Delete failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/instance/{bot_name}/start")
async def start_instance(
    bot_name: str,
    request: Request,
    x_timestamp: str = Header(...),
    x_signature: str = Header(...),
    x_api_key: str = Header(...)
):
    logger.info(f"Received start request for {bot_name}")
    data = {"bot_name": bot_name}
    if not security_manager.verify_signature(data, x_timestamp, x_signature, x_api_key):
        logger.warning("Invalid signature or API key")
        raise HTTPException(status_code=401, detail="Invalid signature or API key")
        
    try:
        instance_path = instance_manager.base_path / bot_name
        if not instance_path.exists():
            raise HTTPException(status_code=404, detail=f"Instance {bot_name} not found")
            
        venv_python = instance_path / "venv/bin/python"
        pm2_command = f"pm2 start {venv_python} --name {bot_name} -- main.py"
        await instance_manager.run_command(pm2_command, cwd=str(instance_path))
        
        logger.info(f"Successfully started instance: {bot_name}")
        return JSONResponse(
            status_code=200,
            content={"status": "success", "message": f"Instance {bot_name} started"}
        )
    except Exception as e:
        logger.error(f"Start failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/instance/{bot_name}/stop")
async def stop_instance(
    bot_name: str,
    request: Request,
    x_timestamp: str = Header(...),
    x_signature: str = Header(...),
    x_api_key: str = Header(...)
):
    logger.info(f"Received stop request for {bot_name}")
    data = {"bot_name": bot_name}
    if not security_manager.verify_signature(data, x_timestamp, x_signature, x_api_key):
        logger.warning("Invalid signature or API key")
        raise HTTPException(status_code=401, detail="Invalid signature or API key")
        
    try:
        pm2_list = await instance_manager.run_command("pm2 list")
        if bot_name not in pm2_list:
            raise HTTPException(status_code=404, detail=f"Instance {bot_name} not running")
            
        await instance_manager.run_command(f"pm2 stop {bot_name}")
        logger.info(f"Successfully stopped instance: {bot_name}")
        return JSONResponse(
            status_code=200,
            content={"status": "success", "message": f"Instance {bot_name} stopped"}
        )
    except Exception as e:
        logger.error(f"Stop failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/instances")
async def list_instances(
    request: Request,
    x_timestamp: str = Header(...),
    x_signature: str = Header(...),
    x_api_key: str = Header(...)
):
    logger.info("Received list instances request")
    data = {}  
    if not security_manager.verify_signature(data, x_timestamp, x_signature, x_api_key):
        logger.warning("Invalid signature or API key")
        raise HTTPException(status_code=401, detail="Invalid signature or API key")
        
    try:
        pm2_json = await instance_manager.run_command("pm2 jlist")
        processes = json.loads(pm2_json)
        
        instances = []
        for proc in processes:
            instances.append({
                "name": proc.get("name"),
                "status": proc.get("pm2_env", {}).get("status"),
                "uptime": proc.get("pm2_env", {}).get("pm_uptime"),
                "restarts": proc.get("pm2_env", {}).get("restart_time"),
                "cpu": proc.get("monit", {}).get("cpu"),
                "memory": proc.get("monit", {}).get("memory"),
                "path": proc.get("pm2_env", {}).get("pm_cwd")
            })
            
        logger.info("Successfully retrieved instance list")
        return JSONResponse(
            status_code=200,
            content={
                "status": "success",
                "instances": instances
            }
        )
    except Exception as e:
        logger.error(f"List failed: {str(e)}")
        raise HTTPException(status_code=500, detail=str(e))

if __name__ == "__main__":
    logger.info("Starting server")
    uvicorn.run(app, host="0.0.0.0", port=8080, log_level="info")
