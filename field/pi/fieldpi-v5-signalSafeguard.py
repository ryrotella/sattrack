def signal_handler(sig, frame):
    """Handle SIGTERM and SIGINT for clean shutdown"""
    global recording_in_progress, current_satellite_code, recording_end_time
    
    logging.info("Shutdown signal received, exiting...")
    
    # Terminate any running process
    if current_process and current_process.poll() is None:
        try:
            os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
            logging.info("Process terminated on shutdown")
        except:
            pass
    
    # Reset recording state
    recording_in_progress = False
    current_satellite_code = None
    recording_end_time = None
    
    # Close serial port if open
    if 'ser' in globals() and ser.is_open:
        ser.close()
    
    sys.exit(0)
    # Serial port configuration
SERIAL_PORT = '/dev/ttyS0'  
BAUD_RATE = 9600
#!/usr/bin/env python3
import os
import signal
import subprocess
import sys
import threading
import time
import datetime
import logging
import serial

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("/home/ftyftty/serial_handler.log"),
        logging.StreamHandler()
    ]
)

# Configuration
RECORDINGS_DIR = "/home/ftyftty/recordings"  # Directory to store recordings
GDRIVE_REMOTE = "gdrive:"               # rclone remote name
GDRIVE_FOLDER = "PiShare"  # Folder in Google Drive
DELETE_AFTER_UPLOAD = True              # Delete local files after successful upload
SHUTDOWN_AFTER_UPLOAD = True            # Shutdown Pi after successful upload

# Command dictionary - maps codes to terminal commands and responses
COMMANDS = {
    "103": {
        "command": "uptime", # Get system uptime
        "response": "103_ACK",
        "needs_termination": False
    },
    "104": {
        "command": "sudo shutdown -h now",
        "response": "104_ACK",
        "needs_termination": False
    },
    "105": {
        "command": "rtl_fm -f 137.62M -s 60k -g 40 -p 55 -E wav -E deemp -F 9 - | sox -t raw -e signed -c 1 -b 16 -r 60000 - noaa15_{timestamp}.wav rate 11025",
        "response": "105_NOAA15",
        "needs_termination": True,
        "output_file": "noaa15_{timestamp}.wav"
    },
    "106": {
        "command": "rtl_fm -f 137.9125M -s 60k -g 40 -p 55 -E wav -E deemp -F 9 - | sox -t raw -e signed -c 1 -b 16 -r 60000 - noaa18_{timestamp}.wav rate 11025",
        "response": "106_NOAA18",
        "needs_termination": True,
        "output_file": "noaa18_{timestamp}.wav"
    },
    "107": {
        "command": "rtl_fm -f 137.10M -s 60k -g 40 -p 55 -E wav -E deemp -F 9 - | sox -t raw -e signed -c 1 -b 16 -r 60000 - noaa19_{timestamp}.wav rate 11025",
        "response": "107_NOAA19",
        "needs_termination": True,
        "output_file": "noaa19_{timestamp}.wav"
    },
    "108": {
        "command": "rtl_fm -f 145.80M -s 48k -g 40 -p 55 - | play -r 48000 -t raw -e signed-int -b 16 -c 1 -V1 -",
        "response": "108_ISS",
        "needs_termination": True,
        "output_file": "iss_{timestamp}.wav"
    },
    # Add more commands as needed
}

# Global process tracking
current_process = None
timer_thread = None
recording_in_progress = False
current_satellite_code = None
recording_end_time = None

def execute_command(cmd, duration=None, needs_termination=False, command_code=None, output_file=None):
    """Execute a shell command and return the output"""
    global current_process, timer_thread, recording_in_progress, current_satellite_code, recording_end_time
    
    try:
        # Special handling for shutdown command
        if "shutdown" in cmd:
            logging.info(f"Executing shutdown command: {cmd}")
            # Return immediately for shutdown command
            subprocess.Popen(cmd, shell=True)
            return "Shutdown initiated"
        
        # For commands that run continuously and need termination (satellite recordings)
        if needs_termination:
            # Check if recording is already in progress
            if recording_in_progress and current_process and current_process.poll() is None:
                # Calculate remaining time for current recording
                now = time.time()
                remaining_seconds = max(0, recording_end_time - now)
                
                logging.warning(f"Recording already in progress for satellite {current_satellite_code}")
                logging.warning(f"Remaining time: {int(remaining_seconds)} seconds")
                
                # Only if this is a different satellite code, log the conflict
                if command_code != current_satellite_code:
                    conflict_message = f"Ignoring new recording request for {command_code} to avoid interrupting current recording of {current_satellite_code}"
                    logging.warning(conflict_message)
                    return f"Cannot start: {conflict_message}"
                else:
                    # Same satellite being requested again
                    return f"Already recording satellite {current_satellite_code} for {int(remaining_seconds)} more seconds"
            
            # No conflict, proceed with recording
            # Kill any existing process (this happens only if recording_in_progress is False)
            if current_process and current_process.poll() is None:
                try:
                    logging.info("Terminating existing process")
                    os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
                except Exception as e:
                    logging.error(f"Error killing previous process: {e}")
            
            # Cancel any existing timer
            if timer_thread and timer_thread.is_alive():
                logging.info("Cancelling existing timer")
                timer_thread.cancel()
            
            # Ensure recordings directory exists
            if output_file and not os.path.exists(RECORDINGS_DIR):
                os.makedirs(RECORDINGS_DIR)
            
            # Generate timestamp for filename
            timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            
            # Replace the {timestamp} placeholder in the command and output filename
            if output_file:
                output_file = output_file.replace("{timestamp}", timestamp)
                full_path = os.path.join(RECORDINGS_DIR, output_file)
                # Replace both the timestamp placeholder and the output file in the command
                cmd = cmd.replace("{timestamp}", timestamp)
                if "{timestamp}" not in cmd:  # If timestamp was not in original command
                    # Replace the output file in the command
                    base_output = os.path.basename(output_file)
                    for part in base_output.split("{timestamp}"):
                        if part in cmd:
                            cmd = cmd.replace(part, os.path.join(RECORDINGS_DIR, part))
                else:
                    # The timestamp was already handled, now handle the path
                    cmd = cmd.replace(output_file, full_path)
            
            # Start the new process
            logging.info(f"Starting command with duration {duration} seconds: {cmd}")
            logging.info(f"Output file will be: {output_file}")
            
            # Run in separate process group so we can kill all child processes later
            current_process = subprocess.Popen(
                cmd, 
                shell=True, 
                preexec_fn=os.setsid,
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE
            )
            
            # Store the output file for upload
            current_output_file = output_file
            
            # Mark recording as in progress
            recording_in_progress = True
            current_satellite_code = command_code
            
            # Calculate when recording will end
            recording_end_time = time.time() + duration if duration else None
            
            # If duration provided, set up a timer to kill the process
            if duration:
                def terminate_and_upload():
                    global recording_in_progress, current_satellite_code, recording_end_time
                    
                    try:
                        if current_process and current_process.poll() is None:
                            logging.info(f"Timer expired after {duration} seconds, terminating process")
                            os.killpg(os.getpgid(current_process.pid), signal.SIGTERM)
                            logging.info("Process terminated")
                            
                            # Wait a moment for files to be properly closed
                            time.sleep(2)
                            
                            # Upload the recording to Google Drive if output_file was specified
                            if current_output_file and command_code:
                                full_path = os.path.join(RECORDINGS_DIR, current_output_file)
                                upload_result = upload_to_gdrive(full_path, command_code)
                                logging.info(f"Upload result: {upload_result}")
                    except Exception as e:
                        logging.error(f"Error in timer thread: {e}")
                    finally:
                        # Reset recording status
                        recording_in_progress = False
                        current_satellite_code = None
                        recording_end_time = None
                
                # Create and start the timer
                timer_thread = threading.Timer(duration, terminate_and_upload)
                timer_thread.daemon = True
                timer_thread.start()
                
            return f"Process started with PID {current_process.pid}, duration {duration} seconds"
        
        # For simple commands that don't run continuously
        else:
            result = subprocess.check_output(cmd, shell=True, text=True, timeout=5)
            logging.info(f"Command executed: {cmd}")
            return result.strip()
            
    except subprocess.TimeoutExpired:
        return "Command timed out"
    except subprocess.CalledProcessError as e:
        logging.error(f"Command failed: {e}")
        return f"Error: {e}"
    except Exception as e:
        logging.error(f"Error executing command: {e}")
        return f"Error: {e}"

def upload_to_gdrive(file_path, command_code):
    """Upload a file to Google Drive using rclone"""
    global recording_in_progress, current_satellite_code, recording_end_time
    
    if not os.path.exists(file_path):
        logging.error(f"File not found for upload: {file_path}")
        return "File not found"
    
    try:
        # Extract just the filename from the path
        filename = os.path.basename(file_path)
        
        # Upload the file with rclone
        upload_command = f"rclone copy '{file_path}' {GDRIVE_REMOTE}{GDRIVE_FOLDER} --drive-shared-with-me"
        logging.info(f"Uploading file to Google Drive: {upload_command}")
        
        result = subprocess.run(upload_command, shell=True, capture_output=True, text=True)
        
        if result.returncode == 0:
            logging.info(f"Successfully uploaded {file_path} to Google Drive")
                # Force reset recording status after successful upload
            recording_in_progress = False
            current_satellite_code = None
            recording_end_time = None
            
            # Delete local file if configured to do so
            if DELETE_AFTER_UPLOAD:
                try:
                    os.remove(file_path)
                    logging.info(f"Deleted local file: {file_path}")
                except Exception as e:
                    logging.error(f"Failed to delete local file: {e}")
            
            # Send notification via serial
            response = f"UPLOAD_SUCCESS:{command_code}:{filename}"
            if 'ser' in globals() and ser.is_open:
                ser.write(f"{response}\n".encode('ascii'))
            
            # Trigger shutdown if configured and no recording is in progress
            if SHUTDOWN_AFTER_UPLOAD and not recording_in_progress:
                logging.info("Initiating shutdown after successful upload...")
                # Notify Arduino first
                if 'ser' in globals() and ser.is_open:
                    ser.write(f"SHUTDOWN_INITIATED:Upload complete\n".encode('ascii'))
                    time.sleep(2)  # Give Arduino time to process the message
                
                # Schedule shutdown with a short delay
                subprocess.Popen("sudo shutdown -h +1", shell=True)
                return f"Uploaded to Google Drive and initiating shutdown"
            
            return f"Uploaded to Google Drive: {filename}"
        else:
            logging.error(f"Failed to upload file: {result.stderr}")
            return f"Upload failed: {result.stderr[:50]}"
    
    except Exception as e:
        logging.error(f"Error during upload: {e}")
        return f"Upload error: {str(e)[:50]}"

def main():
    global ser
    
    # Set up signal handlers for clean shutdown
    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)
    
    # Initial setup
    try:
        # Create recordings directory if it doesn't exist
        if not os.path.exists(RECORDINGS_DIR):
            os.makedirs(RECORDINGS_DIR)
            logging.info(f"Created recordings directory: {RECORDINGS_DIR}")
            
        # Test rclone configuration
        try:
            subprocess.run(
                f"rclone lsd {GDRIVE_REMOTE}", 
                shell=True, 
                check=True, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE
            )
            logging.info("rclone connection to Google Drive verified")
            
            # Create the destination folder if it doesn't exist
            subprocess.run(
                f"rclone mkdir {GDRIVE_REMOTE}{GDRIVE_FOLDER}", 
                shell=True, 
                check=True, 
                stdout=subprocess.PIPE, 
                stderr=subprocess.PIPE
            )
            logging.info(f"Verified Google Drive folder: {GDRIVE_FOLDER}")
        except subprocess.CalledProcessError:
            logging.error("Failed to verify rclone configuration. Google Drive uploads may not work.")
        
        # Open serial connection with explicit settings
        ser = serial.Serial(
            port=SERIAL_PORT,
            baudrate=BAUD_RATE,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=1
        )
        
        logging.info(f"Serial connection opened on {SERIAL_PORT} at {BAUD_RATE} baud")
        
        # Clear any initial data
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        
        # Send ready signal to Arduino
        time.sleep(2)  # Short delay to ensure things are settled
        ready_message = "Pi ready\n"
        ser.write(ready_message.encode('ascii'))
        logging.info("Sent ready signal to Arduino")
        
        # Buffer to store incoming data
        buffer = ""
        
        while True:
            # Read data if available
            if ser.in_waiting > 0:
                try:
                    # Read a line (until newline character)
                    line = ser.readline().decode('ascii', errors='replace').strip()
                    
                    if line:  # Only process non-empty lines
                        logging.info(f"Received command: {line}")
                        
                        # Check if command contains duration (format: "CODE:DURATION")
                        parts = line.split(":")
                        code = parts[0]
                        duration = int(parts[1]) if len(parts) > 1 else None
                        
                        # Process the command
                        if code in COMMANDS:
                            cmd_info = COMMANDS[code]
                            
                            # Check if this is a force stop command (104 - shutdown)
                            if code == "104":
                                # Reset recording status for shutdown
                                recording_in_progress = False
                                current_satellite_code = None
                                recording_end_time = None
                            
                            # Execute the command with duration if provided
                            cmd_output = execute_command(
                                cmd_info["command"], 
                                duration, 
                                cmd_info.get("needs_termination", False),
                                code,
                                cmd_info.get("output_file", None)
                            )
                            
                            # Send response
                            response = f"{cmd_info['response']}:{cmd_output[:50]}\n"
                            ser.write(response.encode('ascii'))
                            logging.info(f"Sent response: {response.strip()}")
                        else:
                            # Unknown command
                            ser.write(f"UNKNOWN_CODE:{line}\n".encode('ascii'))
                            logging.warning(f"Unknown command received: {line}")
                            
                except Exception as e:
                    logging.error(f"Error processing command: {e}")
            
            # Small delay to prevent CPU hogging
            time.sleep(0.1)
            
    except KeyboardInterrupt:
        logging.info("Program terminated by user")
    except Exception as e:
        logging.error(f"Error: {e}")
    finally:
        if 'ser' in locals() and ser.is_open:
            ser.close()
            logging.info("Serial connection closed")

if __name__ == "__main__":
    main()