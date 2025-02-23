import re
import sys
from dataclasses import field
from typing import Tuple, Dict, Any

import requests
import schedule
import time
import datetime
import pytz
import os
from dotenv import load_dotenv
from loguru import logger

logger.remove()
logger.add(sys.stdout, colorize=True, format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>")

class Raid:
    def __init__(self, name, disks):
        self.name = name
        self.state = 'KO'
        self.disks = disks
        self.disks_KO = []

    def state_is_good(self) -> None:
        self.state = 'OK'

# Load environment variables from .env file
load_dotenv()

# Function ############################################################################
def parse_raid_file(file_path):
    raids = []
    with open(file_path, 'r') as file:
        lines = file.readlines()
    file.close()

    line_index = 0
    while line_index < len(lines):
        line = lines[line_index].strip()

        # Search for RAID line
        raid_match = re.match(r'^(md\w+)\s*:\s*active\s+raid\d+\s+(.+)', line)
        if raid_match:
            raid = Raid(raid_match.group(1), raid_match.group(2).split())

            # Move to next line for blocks and state
            line_index += 1
            state_line = lines[line_index].strip()

            # Get disk states (e.g., [UUUU] for all disks in working state)
            state_match = re.search(r'\[([U_]+)\]', state_line)
            if state_match:
                total_disks = len(state_match.group(1))
                good_disks = state_match.group(1).count("U")

                # Check disk states
                if good_disks == total_disks:
                    raid.state_is_good()
                else:
                    for index, disk in enumerate(state_match.group(1)):
                        if disk == "_":
                            raid.disks_KO.append(raid.disks[index])

            raids.append(raid)

        line_index += 1

    return raids

def send_discord_notification(url: str, message_object: Dict[str, Any]) -> bool:
    """Send a message to a Discord Webhook."""
    try:
        response = requests.post(url, json=message_object)
        response.raise_for_status()
        logger.success("Discord notification sent successfully, code {}", response.status_code)
        return True
    except requests.exceptions.HTTPError as err:
        logger.error("Failed to send Discord notification: {}", err)
        return False
    except Exception as err:
        logger.error("Unexpected error sending Discord notification: {}", err)
        return False

def send_ntfy_notification(url: str, title: str, message: str, priority: str = "default", tags: list = None) -> bool:
    """Send a message to an NTFY topic."""
    headers = {
        "Title": title,
        "Priority": priority,
    }
    
    if tags:
        headers["Tags"] = ",".join(tags)

    try:
        response = requests.post(url, 
                               headers=headers,
                               data=message)
        response.raise_for_status()
        logger.success("NTFY notification sent successfully, code {}", response.status_code)
        return True
    except requests.exceptions.HTTPError as err:
        logger.error("Failed to send NTFY notification: {}", err)
        return False
    except Exception as err:
        logger.error("Unexpected error sending NTFY notification: {}", err)
        return False

def discord_factory(raids):
    """Create a discord message from the RAID status."""
    problem_detected = False
    message = {
        "content": "All Raids are good.\n---",
        "embeds": []
    }

    if not raids:
        message["content"] = "No RAID disks found.\n---"
        return message, True

    for raid in raids:
        embed = {}

        if raid.state == "KO":
            message["content"] = "A problem has been detected!\n---"
            embed["title"] = f"{raid.name} :x:"
            embed["description"] = f"At least one disk is down"
            embed["color"] = 16063773
            problem_detected = True
        else:
            embed["title"] = f"{raid.name} :white_check_mark:"
            embed["description"] = "All disks are operational!"
            embed["color"] = 3126294

        embed["fields"] = [
            {
                "name": "RAID state",
                "value": raid.state,
                "inline": True
            },
            {
                "name": "Disks list",
                "value": ', '.join(raid.disks),
                "inline": False
            }
        ]

        if raid.disks_KO:
            embed["fields"].append({
                "name": "Failed disks list",
                "value": ', '.join(raid.disks_KO),
                "inline": False
            })

        embed["footer"] = {
            "text": "CheckMyRaid report"
        }
        embed["timestamp"] = datetime.datetime.now().isoformat()
        message["embeds"].append(embed)

    return message, problem_detected

def ntfy_factory(raids):
    """Create NTFY message from the RAID status."""
    problem_detected = False
    message_parts = []
    
    if not raids:
        return "No RAID disks found.", "warning", ["warning"], False
    
    for raid in raids:
        raid_status = []
        raid_status.append(f"RAID: {raid.name}")
        raid_status.append(f"State: {raid.state}")
        raid_status.append(f"Disks: {', '.join(raid.disks)}")
        
        if raid.disks_KO:
            problem_detected = True
            raid_status.append(f"Failed disks: {', '.join(raid.disks_KO)}")
            
        message_parts.append("\n".join(raid_status))
    
    full_message = "\n\n".join(message_parts)
    
    if problem_detected:
        title = "RAID Problem Detected!"
        priority = "high"
        tags = ["warning", "raid", "error"]
    else:
        title = "RAID Status: All Systems Normal"
        priority = "default"
        tags = ["success", "raid"]
        
    return title, full_message, priority, tags, problem_detected

def send_notifications(raids):
    """Send notifications to all configured endpoints."""
    discord_url = os.getenv('DISCORD_WEBHOOK_URL')
    ntfy_url = os.getenv('NTFY_URL')
    notifications_sent = []

    # Send Discord notification if configured
    if discord_url:
        discord_message, problem_detected = discord_factory(raids)
        if send_discord_notification(discord_url, discord_message):
            notifications_sent.append("Discord")

    # Send NTFY notification if configured
    if ntfy_url:
        title, message, priority, tags, problem_detected = ntfy_factory(raids)
        if send_ntfy_notification(ntfy_url, title, message, priority, tags):
            notifications_sent.append("NTFY")

    if notifications_sent:
        logger.info("Notifications sent to: {}", ", ".join(notifications_sent))
    else:
        logger.warning("No notifications were sent. Check your endpoint configurations.")

    return problem_detected

def main():
    # Variables
    mdstat_file = "/app/data/mdstat"

    # Read the content of the mdstat file
    raids = parse_raid_file(mdstat_file)

    # Send notifications and get problem status
    problem_detected = send_notifications(raids)

    if problem_detected:
        logger.warning("Anomalies detected in at least one raid array.")
    else:
        logger.info("All Raids are OK.")

#######################################################################################
# Start the script 1 time if the variable CHECK_ON_STARTUP is set to True
if os.getenv('CHECK_ON_STARTUP') == "True":
    logger.info("Checking RAID on startup of the container")
    main()

# Get the timezone and the schedule time
timezone = os.getenv('TZ', 'UTC').replace('"', '').replace("'", '')
trigerAt = os.getenv('TRIGER_SCHEDULE_AT', '12:00').replace('"', '').replace("'", '')

# Get the current date and time on the timezone
tz = pytz.timezone(timezone)
now = datetime.datetime.now(tz)

logger.info("Date and time is currently on the timezone {}: {}", 
           timezone, now.strftime("%Y-%m-%d %H:%M:%S"))
logger.info("Next raid check scheduled at {}", trigerAt)

# Schedule the script to run at a specific time
schedule.every().day.at(trigerAt, timezone).do(main)

# Schedule loop
while True:
    schedule.run_pending()
    time.sleep(10)