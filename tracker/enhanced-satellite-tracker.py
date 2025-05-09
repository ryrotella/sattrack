#!/usr/bin/env python3

import os
import random
import time
import json
import ephem
import datetime
import argparse
import subprocess
from pathlib import Path
import paho.mqtt.client as mqtt
import logging
import sys
import urllib.request

class EnhancedSatelliteTracker:
    def __init__(self, config_file=None):
        # Set up logging
        logging.basicConfig(
            level=logging.INFO,
            format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
            handlers=[
                logging.FileHandler("/Users/ryanrotella/satellite/sat_tracker.log"),
                logging.StreamHandler(sys.stdout)
            ]
        )
        self.logger = logging.getLogger("SatelliteTracker")
        
        if config_file:
            self.load_config(config_file)
        else:
            # Default configuration with Brooklyn coordinates
            self.config = {
                "satellites": "AUTO",  # Auto-discover all trackable satellites
                "min_elevation": 20,  # Minimum elevation in degrees
                "min_duration": 300,  # Minimum pass duration in seconds (5 minutes)
                "check_interval": 60,  # Seconds between checks
                "prediction_window": 24,  # Hours to look ahead for predictions
                "log_file": "/Users/ryanrotella/satellite/sat_tracker.log",
                "observer": {
                    "lat": 40.699484,
                    "lon": -73.974255,
                    "elevation": 10
                },
                "satellite_categories": {
                    "NOAA": True,
                    "METEOR": True,
                    "AMSAT": True,
                    "ISS": True,
                    "GOES": False,  # Requires specialized equipment
                    "OTHER": True
                },
                "satellite_frequencies": {
                    "NOAA-15": 137.62e6,
                    "NOAA-18": 137.9125e6,
                    "NOAA-19": 137.1e6,
                    "METEOR-M 2": 137.1e6,
                    "METEOR-M2 2": 137.9e6,
                    "ISS": 145.8e6,
                    "ISS-APRS": 145.825e6,
                    "AO-91": 145.96e6,
                    "AO-92": 145.88e6,
                    "SO-50": 436.795e6
                },
                "mqtt": {
                    "broker": "localhost",
                    "port": 1883,
                    "username": "",
                    "password": "",
                    "topic_prefix": "satellite/",
                    "client_id": "sat_tracker",
                    "power_control_topic": "arduino/power"
                }
            }
        
        # Create observer for PyEphem calculations
        self.observer = ephem.Observer()
        self.update_observer()
        
        # Initialize tracking state
        self.tracked_passes = {}  # Track specific passes
        self.scheduled_passes = []
        self.next_pass_time = None
        
        # Discover TLE files and satellite lists
        self.discover_satellites()
        
        # Initialize MQTT client
        self.init_mqtt()
    
    def load_config(self, config_file):
        """Load configuration from a JSON file"""
        try:
            with open(config_file, 'r') as f:
                self.config = json.load(f)
            self.logger.info(f"Configuration loaded from {config_file}")
        except Exception as e:
            self.logger.error(f"Error loading configuration: {e}")
            self.logger.info("Using default configuration")
    
    def init_mqtt(self):
        """Initialize MQTT client and connection with robust reconnection for Shiftr.io"""
        base_id = self.config['mqtt']['client_id']
        unique_suffix = ''.join(random.choices('0123456789abcdef', k=8))
        client_id = f"{base_id}_{unique_suffix}"
        self.logger.info(f"Initializing MQTT with client ID: {client_id}")
        
        # Initialize the client with VERSION2 for paho-mqtt 2.1.0
        try:
            # For paho-mqtt 2.x
            self.mqtt_client = mqtt.Client(
                client_id=client_id, 
                clean_session=True,
                callback_api_version=CallbackAPIVersion.VERSION2
            )
        except Exception as e:
            self.logger.error(f"Error initializing MQTT client: {e}")
            self.logger.info("Will attempt to initialize with fallback options")
            try:
                # Fallback to basic initialization 
                self.mqtt_client = mqtt.Client(client_id=client_id)
            except Exception as e2:
                self.logger.error(f"Fallback initialization also failed: {e2}")
                raise
        
        # Define connection state tracking
        self.mqtt_connected = False
        self.reconnect_count = 0
        self.max_reconnect_count = 20  # Higher limit for persistent retries
        self.reconnect_delay = 1  # Initial delay in seconds
        
        # Configure credentials if provided
        if self.config["mqtt"]["username"]:
            self.mqtt_client.username_pw_set(
                self.config["mqtt"]["username"], 
                self.config["mqtt"]["password"]
            )
        
        # Set up callbacks with more robust error handling
        def on_connect(client, userdata, flags, reason_code, properties=None):
            if reason_code == 0:
                self.mqtt_connected = True
                # self.reconnect_count = 0  # Reset reconnect count on successful connection
                # self.reconnect_delay = 1  # Reset delay
                self.logger.info("Connected to MQTT broker")
                
                # Subscribe to command responses for two-way communication
                try:
                    client.subscribe(f"{self.config['mqtt']['topic_prefix']}response/#")
                    # Subscribe to Arduino status topic if configured
                    if "power_control_topic" in self.config["mqtt"]:
                        client.subscribe(f"{self.config['mqtt']['power_control_topic']}/status")
                except Exception as e:
                    self.logger.error(f"Error subscribing to topics: {e}")
            else:
                self.mqtt_connected = False
                error_messages = {
                    1: "Connection refused - incorrect protocol version",
                    2: "Connection refused - invalid client identifier",
                    3: "Connection refused - server unavailable",
                    4: "Connection refused - bad username or password",
                    5: "Connection refused - not authorized"
                }
                error_msg = error_messages.get(reason_code, f"Unknown error code: {reason_code}")
                self.logger.error(f"Failed to connect to MQTT broker: {error_msg}")
        
        def on_disconnect(client, userdata, reason_code, properties=None):
            if reason_code != 0:
                self.logger.warning(f"Unexpected disconnection from MQTT broker with code: {reason_code}")
                self.mqtt_connected = False
                
                # For error code 7 specifically
                if reason_code == 7:
                    self.logger.info("Connection lost (code 7), will attempt to reconnect with delay")
                    # Let the automatic reconnect handle it with a delay
                    time.sleep(2)
            else:
                self.logger.info("Disconnected from MQTT broker (expected)")
                self.mqtt_connected = False
        
        def on_message(client, userdata, msg, properties=None):
            try:
                topic = msg.topic
                payload_str = msg.payload.decode('utf-8')
                
                # Try to parse as JSON, but handle non-JSON payloads gracefully
                try:
                    payload = json.loads(payload_str)
                except json.JSONDecodeError:
                    # If not valid JSON, use the raw string as payload
                    self.logger.debug(f"Received non-JSON payload on topic {topic}")
                    payload = payload_str
                    
                self.handle_message(topic, payload)
            except Exception as e:
                self.logger.error(f"Error handling message on topic {msg.topic}: {e}")
        
        # Configure client callbacks
        self.mqtt_client.on_connect = on_connect
        self.mqtt_client.on_disconnect = on_disconnect
        self.mqtt_client.on_message = on_message
        
        # Set up automatic reconnect with exponential backoff
        self.mqtt_client.reconnect_delay_set(
            min_delay=1, 
            max_delay=self.config["mqtt"].get("reconnect_delay", 120)  # Increased max delay
        )
        
        # Use a smaller, simpler last will message
        status_topic = f"{self.config['mqtt']['topic_prefix']}status"
        will_payload = json.dumps({
            "status": "offline", 
            "timestamp": datetime.datetime.now().isoformat()
        })
        
        # Set last will message with QoS 0 for reliability
        self.mqtt_client.will_set(
            status_topic,
            will_payload,
            qos=0,  # Changed from QoS 1 to QoS 0 for less overhead
            retain=False
        )
        
        # Configure TLS if using Shiftr.io
        if "shiftr.io" in self.config["mqtt"]["broker"]:
            self.logger.info("Configuring TLS for Shiftr.io connection")
            try:
                self.mqtt_client.tls_set()  # Use default CA certificates
                # Update port for TLS if not already set
                if self.config["mqtt"]["port"] == 1883:
                    self.config["mqtt"]["port"] = 8883
                    self.logger.info(f"Updated port to {self.config['mqtt']['port']} for TLS connection")
            except Exception as e:
                self.logger.error(f"Error setting up TLS: {e}")
        
        # Connect to the broker
        try:
            self.logger.info(f"Connecting to MQTT broker at {self.config['mqtt']['broker']}:{self.config['mqtt']['port']}")
            
            # Get keepalive value from config or use default (increased for better stability)
            keepalive = self.config["mqtt"].get("keepalive", 120)  # Increased from 60 to 120
            
            # Connect with configured parameters
            self.mqtt_client.connect_async(
                self.config["mqtt"]["broker"],
                self.config["mqtt"]["port"],
                keepalive=keepalive
            )
            
            # Start the network loop in a background thread
            self.mqtt_client.loop_start()
            
            # Store loop start time
            self.mqtt_loop_start_time = time.time()
            
            self.logger.info("MQTT loop started in background thread")
            
        except Exception as e:
            self.logger.error(f"Error connecting to MQTT broker: {e}")
            self.logger.info("Will continue running and try to reconnect later")
        
    def on_message(self, client, userdata, msg):
        """Handle incoming MQTT messages"""
        try:
            topic = msg.topic
            payload = json.loads(msg.payload.decode('utf-8'))
            self.handle_message(topic, payload)
        except json.JSONDecodeError:
            self.logger.error(f"Error decoding JSON from topic {msg.topic}")
        except Exception as e:
            self.logger.error(f"Error handling message: {e}")
    
    def update_observer(self):
        """Update the observer with configuration values"""
        self.observer.lat = str(self.config["observer"]["lat"])
        self.observer.lon = str(self.config["observer"]["lon"])
        self.observer.elevation = self.config["observer"]["elevation"]
        self.observer.date = datetime.datetime.now(datetime.UTC)
    
    def discover_satellites(self):
        """Discover available satellites from TLE files"""
        self.logger.info("Discovering available satellites from TLE data...")
        
        # First ensure TLE data is updated
        self.update_tle_data()
        
        # Get the satellites directory
        home = str(Path.home())
        gpredict_data = os.path.join(home, ".config/Gpredict/satellites")
        
        if not os.path.exists(gpredict_data):
            self.logger.error(f"Satellite data directory not found: {gpredict_data}")
            return
        
        # Initialize satellite dictionary and lists
        self.satellites = {}
        all_satellites = []
        
        # Process TLE files and collect satellite names
        try:
            tle_files = os.listdir(gpredict_data)
            for tle_file in tle_files:
                tle_path = os.path.join(gpredict_data, tle_file)
                try:
                    with open(tle_path, 'r') as f:
                        lines = f.readlines()
                        i = 0
                        while i < len(lines) - 2:
                            name = lines[i].strip()
                            line1 = lines[i+1].strip()
                            line2 = lines[i+2].strip()
                            
                            # Add satellite to our list
                            all_satellites.append({
                                "name": name,
                                "line1": line1,
                                "line2": line2,
                                "category": self.categorize_satellite(name),
                                "file": tle_file
                            })
                            i += 3
                except Exception as e:
                    self.logger.error(f"Error reading TLE file {tle_file}: {e}")
        except Exception as e:
            self.logger.error(f"Error listing TLE files: {e}")
        
        self.logger.info(f"Discovered {len(all_satellites)} satellites in TLE data")
        
        # Filter satellites based on configuration
        self.filter_and_load_satellites(all_satellites)
    
    def categorize_satellite(self, name):
        """Categorize satellite based on name"""
        name_upper = name.upper()
        
        if "NOAA" in name_upper:
            return "NOAA"
        elif "METEOR" in name_upper:
            return "METEOR"
        elif name_upper == "ISS" or "ISS (ZARYA)" in name_upper:
            return "ISS"
        elif "AO-" in name_upper or "SO-" in name_upper or "FO-" in name_upper or "XW-" in name_upper:
            return "AMSAT"
        elif "GOES" in name_upper:
            return "GOES"
        else:
            return "OTHER"
    
    def filter_and_load_satellites(self, all_satellites):
        """Filter and load satellites based on configuration"""
        filtered_satellites = []
        
        # If specific satellites are provided in config, use those
        if isinstance(self.config["satellites"], list):
            for sat_info in all_satellites:
                if sat_info["name"] in self.config["satellites"]:
                    filtered_satellites.append(sat_info)
        
        # If AUTO is specified, use categories to filter
        elif self.config["satellites"] == "AUTO":
            categories = self.config["satellite_categories"]
            for sat_info in all_satellites:
                category = sat_info["category"]
                if category in categories and categories[category]:
                    filtered_satellites.append(sat_info)
        
        # Load the filtered satellites
        for sat_info in filtered_satellites:
            try:
                sat = ephem.readtle(sat_info["name"], sat_info["line1"], sat_info["line2"])
                self.satellites[sat_info["name"]] = {
                    "obj": sat,
                    "category": sat_info["category"],
                    "frequency": self.get_satellite_frequency(sat_info["name"]),
                    "mode": self.get_satellite_mode(sat_info["name"])
                }
                self.logger.debug(f"Loaded satellite: {sat_info['name']} ({sat_info['category']})")
            except Exception as e:
                self.logger.error(f"Error loading TLE for {sat_info['name']}: {e}")
        
        self.logger.info(f"Loaded {len(self.satellites)} satellites for tracking")
    
    def get_satellite_frequency(self, name):
        """Get the frequency for a satellite"""
        frequencies = self.config.get("satellite_frequencies", {})
        # Try direct lookup
        if name in frequencies:
            return frequencies[name]
        
        # Try pattern matching
        for sat_name, freq in frequencies.items():
            if sat_name in name or name in sat_name:
                return freq
        
        # Default frequencies by category
        name_upper = name.upper()
        if "NOAA" in name_upper:
            return 137.5e6  # Generic NOAA APT frequency
        elif "METEOR" in name_upper:
            return 137.1e6  # Generic Meteor LRPT frequency
        elif "ISS" in name_upper:
            return 145.8e6  # ISS voice frequency
        elif any(prefix in name_upper for prefix in ["AO-", "SO-", "FO-", "XW-"]):
            return 145.9e6  # Generic amateur satellite frequency
        
        # Unknown
        return 0
    
    def get_satellite_mode(self, name):
        """Get the operating mode for a satellite"""
        name_upper = name.upper()
        
        if "NOAA" in name_upper:
            return "apt"
        elif "METEOR" in name_upper:
            return "lrpt"
        elif "ISS" in name_upper:
            if "APRS" in name_upper:
                return "aprs"
            return "voice"
        elif any(prefix in name_upper for prefix in ["AO-", "SO-", "FO-", "XW-"]):
            return "linear"  # Generic amateur satellite mode
        
        # Unknown
        return "unknown"
    
    def update_tle_data(self):
        """Update TLE data using direct download instead of Gpredict CLI"""
        try:
            self.logger.info("Updating TLE data directly from celestrak.org...")
            
            # These URLs match Gpredict's sources
            sources = {
                "weather.txt": "https://celestrak.org/NORAD/elements/gp.php?GROUP=weather&FORMAT=tle",
                "noaa.txt": "https://celestrak.org/NORAD/elements/gp.php?GROUP=noaa&FORMAT=tle",
                "amateur.txt": "https://celestrak.org/NORAD/elements/gp.php?GROUP=amateur&FORMAT=tle",
                "stations.txt": "https://celestrak.org/NORAD/elements/gp.php?GROUP=stations&FORMAT=tle",
                "cubesat.txt": "https://celestrak.org/NORAD/elements/gp.php?GROUP=cubesat&FORMAT=tle",
                "goes.txt": "https://celestrak.org/NORAD/elements/gp.php?GROUP=goes&FORMAT=tle"
            }
            
            # Create directories with correct path
            home = str(Path.home())
            gpredict_data = os.path.join(home, ".config/Gpredict/satellites")
            os.makedirs(gpredict_data, exist_ok=True)
            
            success_count = 0
            for filename, url in sources.items():
                try:
                    self.logger.info(f"Downloading {filename} from {url}")
                    output_path = os.path.join(gpredict_data, filename)
                    
                    # Download with timeout and retry
                    max_retries = 3
                    for attempt in range(max_retries):
                        try:
                            with urllib.request.urlopen(url, timeout=30) as response:
                                tle_data = response.read()
                            
                            # Verify we got valid data (should contain TLE lines)
                            if b"1 " in tle_data and b"2 " in tle_data:
                                with open(output_path, 'wb') as f:
                                    f.write(tle_data)
                                success_count += 1
                                self.logger.info(f"Successfully downloaded {filename}")
                                break
                            else:
                                self.logger.warning(f"Downloaded {filename} but data appears invalid")
                                if attempt < max_retries - 1:
                                    time.sleep(5)  # Wait before retry
                        except urllib.error.URLError as e:
                            self.logger.warning(f"Attempt {attempt+1} failed for {filename}: {e}")
                            if attempt < max_retries - 1:
                                time.sleep(5)  # Wait before retry
                except Exception as e:
                    self.logger.error(f"Error downloading {filename}: {e}")
            
            self.logger.info(f"TLE data updated: {success_count}/{len(sources)} files")
            return success_count > 0
        except Exception as e:
            self.logger.error(f"Error updating TLE data: {e}")
            return False
    
    def predict_passes(self):
        """Predict upcoming satellite passes"""
        self.logger.info(f"Predicting satellite passes for the next {self.config['prediction_window']} hours...")
        
        # Update observer with current time
        self.update_observer()
        
        # Calculate the end of our prediction window
        end_time = datetime.datetime.now(datetime.UTC) + datetime.timedelta(hours=self.config["prediction_window"])
        
        # Clear the existing scheduled passes
        self.scheduled_passes = []
        
        for sat_name, sat_data in self.satellites.items():
            try:
                # Set observer time to now for initial prediction
                self.observer.date = datetime.datetime.now(datetime.UTC)
                
                # Get the satellite object
                sat = sat_data["obj"]
                
                # Predict passes until we reach the end of our window
                pass_count = 0
                
                while True:
                    try:
                        next_pass = self.observer.next_pass(sat)
                        
                        # Extract the pass details
                        rise_time = ephem.localtime(next_pass[0])
                        max_time = ephem.localtime(next_pass[2])
                        set_time = ephem.localtime(next_pass[4])
                        max_elevation = next_pass[3] * 180 / 3.14159  # Convert to degrees
                        duration = (next_pass[4] - next_pass[0]) * 24 * 60 * 60  # Duration in seconds
                        
                        # Check if the pass meets our criteria and is within our window
                        if (max_elevation > self.config["min_elevation"] and 
                            duration > self.config["min_duration"] and
                            set_time.replace(tzinfo=None) < end_time.replace(tzinfo=None)):
                            
                            # Generate a unique pass ID
                            pass_id = f"{sat_name}_{rise_time.strftime('%Y%m%d_%H%M%S')}"
                            
                            # Calculate priority based on elevation and duration
                            priority = self.calculate_pass_priority(max_elevation, duration, sat_name, sat_data)
                            
                            # Add pass to our schedule
                            pass_info = {
                                "id": pass_id,
                                "satellite": sat_name,
                                "category": sat_data["category"],
                                "frequency": sat_data["frequency"],
                                "mode": sat_data["mode"],
                                "rise_time": rise_time.isoformat(),
                                "max_time": max_time.isoformat(),
                                "set_time": set_time.isoformat(),
                                "max_elevation": max_elevation,
                                "duration": duration,
                                "priority": priority
                            }
                            
                            self.scheduled_passes.append(pass_info)
                            self.logger.info(f"Scheduled pass for {sat_name} at {rise_time.strftime('%Y-%m-%d %H:%M:%S')}, "
                                    f"max elevation: {max_elevation:.1f}°, duration: {duration/60:.1f}min")
                            
                            pass_count += 1
                        
                        # Set observer time to just after this pass for next prediction
                        self.observer.date = ephem.Date(next_pass[4] + ephem.minute)
                        
                        # If we've gone past our window, break
                        if self.observer.date > ephem.Date(end_time):
                            break
                            
                    except Exception as e:
                        self.logger.error(f"Error predicting pass for {sat_name}: {e}")
                        break
                
                if pass_count > 0:
                    self.logger.info(f"Found {pass_count} passes for {sat_name}")
                        
            except Exception as e:
                self.logger.error(f"Error in pass prediction for {sat_name}: {e}")
        
        # Sort passes by time
        self.scheduled_passes.sort(key=lambda x: x["rise_time"])
        
        # Add human-readable times for better logging
        for pass_info in self.scheduled_passes:
            rise_dt = datetime.datetime.fromisoformat(pass_info["rise_time"])
            set_dt = datetime.datetime.fromisoformat(pass_info["set_time"])
            pass_info["readable_time"] = f"{rise_dt.strftime('%Y-%m-%d %H:%M')} to {set_dt.strftime('%H:%M')}"
        
        # Log the complete schedule
        self.logger.info("Complete pass schedule:")
        for idx, pass_info in enumerate(self.scheduled_passes):
            self.logger.info(f"{idx+1}. {pass_info['satellite']} ({pass_info['category']}) - {pass_info['readable_time']} - "
                        f"Max elevation: {pass_info['max_elevation']:.1f}° - Priority: {pass_info['priority']}")
        
        # Use the new optimized schedule publishing method instead of direct publishing
        self.publish_schedule()
        
        # Return the number of passes found
        return len(self.scheduled_passes)
    
    def calculate_pass_priority(self, elevation, duration, satellite, sat_data):
        """Calculate priority score for a pass based on quality"""
        # Higher elevations and longer durations get higher priority
        # Scale from 1-10 where 10 is highest priority
        elevation_score = min(10, elevation / 10)  # 10 = 90+ degrees
        duration_score = min(10, duration / 120)  # 10 = 20+ minutes
        
        # Base multiplier by category
        category_priority = {
            "NOAA": 1.2,    # NOAA satellites are easy to receive
            "METEOR": 1.5,  # METEOR has higher resolution imagery
            "ISS": 1.3,     # ISS is interesting for various reasons
            "AMSAT": 1.0,   # Amateur radio satellites
            "GOES": 0.8,    # GOES requires specialized equipment
            "OTHER": 0.7    # Other satellites may not have useful signals
        }
        
        # Get category
        category = sat_data["category"]
        
        # Apply category multiplier
        cat_multiplier = category_priority.get(category, 1.0)
        
        # Calculate overall score (0-10)
        score = ((elevation_score * 0.6) + (duration_score * 0.4)) * cat_multiplier
        
        return round(score, 1)
    
    def check_upcoming_passes(self):
        """Check if there are any upcoming passes that need to be prepared for"""
        if not self.scheduled_passes:
            return False
            
        # Get current time with timezone info
        now = datetime.datetime.now(datetime.timezone.utc).astimezone()
        now_str = now.isoformat()
        
        # Find the next upcoming pass
        next_pass = None
        for pass_info in self.scheduled_passes:
            if pass_info["rise_time"] > now_str:
                next_pass = pass_info
                break
        
        if next_pass:
            # Calculate time until pass with proper timezone handling
            rise_time = datetime.datetime.fromisoformat(next_pass["rise_time"])
            
            # If rise_time doesn't have timezone info, add it
            if rise_time.tzinfo is None:
                # Add the same timezone as now
                rise_time = rise_time.replace(tzinfo=now.tzinfo)
            
            time_until_pass = (rise_time - now).total_seconds()
            
            # Check if this is a new pass to prepare for
            if self.next_pass_time != next_pass["rise_time"]:
                self.logger.info(f"Next pass: {next_pass['satellite']} ({next_pass['category']}) "
                            f"in {time_until_pass/60:.1f} minutes")
                self.next_pass_time = next_pass["rise_time"]
            
            # If the pass is soon, send power-on command to Arduino
            if time_until_pass < 2 * 60 and time_until_pass > 0:  # 2 minutes before pass
                self.prepare_for_pass(next_pass, time_until_pass)
                return True
                
        return False
    
    def prepare_for_pass(self, pass_info, time_until_pass):
        """Prepare the system for an upcoming pass"""
        pass_id = pass_info["id"]
        satellite_name = pass_info["satellite"]
        
        # Check if we've already prepared for this pass
        if pass_id in self.tracked_passes:
            return
        
        # Check if this satellite should trigger notifications
        should_notify = False
        cmd_code = 0
        if "notification_satellites" in self.config:
            # If notification_satellites is defined, check if this satellite is in the list
            should_notify = satellite_name in self.config["notification_satellites"]

            if satellite_name == "NOAA 15":
                cmd_code = 105
            
            elif satellite_name == "NOAA 18":
                cmd_code = 106
            
            elif satellite_name == "NOAA 19":
                cmd_code = 107
            
            elif satellite_name == "ISS (ZARYA)":
                cmd_code = 108



        else:
            # If notification_satellites is not defined, notify for all passes (legacy behavior)
            should_notify = True
        
        # Log the preparation but only notify if it's in the notification list
        self.logger.info(f"Preparing for pass {pass_id} in {time_until_pass/60:.1f} minutes")
        
        # Calculate optimal start time (a few minutes before actual rise)
        start_time = datetime.datetime.fromisoformat(pass_info["rise_time"]) - datetime.timedelta(minutes=2)
        end_time = datetime.datetime.fromisoformat(pass_info["set_time"]) + datetime.timedelta(minutes=1)
        
        # Create the pass parameters
        pass_params = {
            "id": pass_id,
            "satellite": satellite_name,
            "category": pass_info["category"],
            "frequency": pass_info["frequency"],
            "mode": pass_info["mode"],
            "start_time": start_time.isoformat(),
            "end_time": end_time.isoformat(),
            "max_elevation": pass_info["max_elevation"],
            "duration": pass_info["duration"]
        }
        
        # Send MQTT notifications only for selected satellites
        if should_notify:
            self.logger.info(f"Sending MQTT notifications for {satellite_name} pass")
            
            # Send power-on command to Arduino if configured
            if "power_control_topic" in self.config["mqtt"]:
                power_message = {
                    "command": "power_on",
                    "reason": f"Preparing for {satellite_name} pass",
                    "code": cmd_code,
                    "scheduled_time": start_time.isoformat(),
                    "duration_estimate": int(pass_info["duration"])  
                    # Add 3 minutes for setup - 180 - JK
                }
                
                self.publish_message(self.config["mqtt"]["power_control_topic"], power_message)
            
            # Schedule the pass with the field Pi
            schedule_message = {
                "command": "schedule_pass",
                "pass": pass_params
            }
            
            self.publish_message("command", schedule_message)
        else:
            self.logger.info(f"Skipping MQTT notifications for {satellite_name} (not in notification list)")
        
        # Add to tracked passes regardless of notification status
        self.tracked_passes[pass_id] = {
            "pass_info": pass_info,
            "prepared": True,
            "completed": False,
            "scheduled_start": start_time.isoformat(),
            "notified": should_notify
        }
    
    def publish_message(self, topic, message):
        # Format topic if needed
        if isinstance(topic, str) and not topic.startswith(self.config['mqtt']['topic_prefix']):
            if "power_control_topic" in self.config['mqtt'] and topic == self.config['mqtt']['power_control_topic']:
                # Don't modify power control topic
                pass
            else:
                topic = f"{self.config['mqtt']['topic_prefix']}{topic}"
        
        # Convert message to JSON if it's not already a string
        if not isinstance(message, str):
            message_json = json.dumps(message)
        else:
            message_json = message
        
        # Check payload size against Shiftr.io's 64KB limit
        max_size = self.config['mqtt'].get('max_payload_size', 65000)
        payload_size = len(message_json.encode('utf-8'))
        
        if payload_size > max_size:
            self.logger.warning(f"Message for {topic} too large ({payload_size} bytes), truncating")
            
            # Handle schedule message specially
            if isinstance(message, dict) and "passes" in message and isinstance(message["passes"], list):
                # Trim passes until it fits
                while len(json.dumps(message).encode('utf-8')) > max_size and len(message["passes"]) > 0:
                    message["passes"].pop()
                message_json = json.dumps(message)
            else:
                # Simple truncation
                message_json = message_json[:max_size]
        
        # Get QoS from config or use safe default for Shiftr.io
        qos = self.config['mqtt'].get('qos', 0)
        
        # Publish with appropriate settings
        try:
            result = self.mqtt_client.publish(
                topic,
                message_json,
                qos=qos,
                retain=False
            )
            
            if result.rc != mqtt.MQTT_ERR_SUCCESS:
                self.logger.error(f"Failed to publish to {topic}, result code: {result.rc}")
                return False
            
            return True
        except Exception as e:
            self.logger.error(f"Error publishing to MQTT topic {topic}: {e}")
            return False
        
    def publish_schedule(self):
        """Publish satellite pass schedule with size optimization"""
        # Start with a small subset of passes (next 10-20)
        limited_passes = self.scheduled_passes[:15]
        
        # Remove unnecessary fields to reduce size
        simplified_passes = []
        for pass_info in limited_passes:
            simplified = {
                "id": pass_info["id"],
                "satellite": pass_info["satellite"],
                "category": pass_info["category"],
                "rise_time": pass_info["rise_time"],
                "set_time": pass_info["set_time"],
                "max_elevation": pass_info["max_elevation"],
                "priority": pass_info["priority"]
            }
            simplified_passes.append(simplified)
        
        # Prepare and publish the smaller message
        schedule_message = {
            "passes": simplified_passes,
            "total_passes": len(self.scheduled_passes),
            "updated": datetime.datetime.now().isoformat(),
            "location": {
                "lat": self.config["observer"]["lat"],
                "lon": self.config["observer"]["lon"]
            }
        }
        
        return self.publish_message("schedule", schedule_message)
    
    def publish_large_data(self, topic, data, batch_size=10):
        """Publish large datasets by splitting into multiple messages"""
        if not isinstance(data, list):
            # If not a list, just publish normally
            return self.publish_message(topic, data)
        
        # Split list into batches
        total_items = len(data)
        total_batches = (total_items + batch_size - 1) // batch_size
        
        self.logger.info(f"Publishing {total_items} items to {topic} in {total_batches} batches")
        
        for i in range(0, total_items, batch_size):
            batch = data[i:i+batch_size]
            batch_num = (i // batch_size) + 1
            
            batch_message = {
                "batch": batch_num,
                "total_batches": total_batches,
                "data": batch
            }
            
            success = self.publish_message(f"{topic}/batch", batch_message)
            if not success:
                self.logger.error(f"Failed to publish batch {batch_num} to {topic}")
                return False
            
            # Short delay between batches to avoid overwhelming the broker
            time.sleep(0.5)
        
        # Send completion message
        completion = {
            "status": "complete",
            "total_items": total_items,
            "total_batches": total_batches,
            "timestamp": datetime.datetime.now().isoformat()
        }
        return self.publish_message(f"{topic}/complete", completion)
    
    def run(self):
        """Main loop to continuously check for satellite passes"""
        self.logger.info("Starting enhanced satellite tracker...")
        
        try:
            # Initial prediction
            num_passes = self.predict_passes()
            self.logger.info(f"Found {num_passes} passes for the next {self.config['prediction_window']} hours")
            
            last_prediction_time = datetime.datetime.now()
            last_tle_update_time = last_prediction_time
            
            while True:
                # Check for upcoming passes
                self.check_upcoming_passes()
                
                current_time = datetime.datetime.now()
                
                # Update TLE data daily
                if (current_time - last_tle_update_time).total_seconds() > 24 * 3600:
                    self.logger.info("Performing daily TLE update")
                    self.update_tle_data()
                    self.discover_satellites()  # Rediscover satellites with new TLE data
                    last_tle_update_time = current_time
                    
                    # Force prediction update after TLE update
                    self.predict_passes()
                    last_prediction_time = current_time
                
                # Re-predict passes every 6 hours or when we have none scheduled
                elif (current_time - last_prediction_time).total_seconds() > 6 * 3600 or not self.scheduled_passes:
                    self.predict_passes()
                    last_prediction_time = current_time
                
                time.sleep(self.config["check_interval"])
                
        except KeyboardInterrupt:
            self.logger.info("Satellite tracker stopped by user")
            # Publish offline status
            self.publish_message("status", {"status": "offline", "timestamp": datetime.datetime.now().isoformat()})
            self.mqtt_client.loop_stop()
            self.mqtt_client.disconnect()
        except Exception as e:
            self.logger.error(f"Error in main loop: {e}")
            # Try to publish error status
            try:
                self.publish_message("status", {"status": "error", "message": str(e), "timestamp": datetime.datetime.now().isoformat()})
            except:
                pass

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Enhanced satellite tracker that automatically discovers trackable satellites")
    parser.add_argument("-c", "--config", help="Path to configuration file")
    args = parser.parse_args()
    
    tracker = EnhancedSatelliteTracker(args.config)
    tracker.run()
