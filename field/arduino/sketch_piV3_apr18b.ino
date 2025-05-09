#include <WiFiNINA.h>
#include <ArduinoMqttClient.h>
#include <ArduinoJson.h>
#include "arduino_secrets.h"

// MQTT setup
WiFiClient wifi;
MqttClient mqttClient(wifi);

// MQTT connection details
char broker[] = "energyctrlserver.cloud.shiftr.io";
int port = 1883;
char topic[] = "status";
String clientID = "Arduino";

// Pi communication state
bool piPowered = false;
bool piReady = false;
// Variables to store pending command
String pendingCommand = "";
bool hasPendingCommand = false;

// Text buffer for receiving messages
char textBuffer[128];
int textBufferIndex = 0;

void setup() {
  pinMode(12, OUTPUT);  // For Pi power control
  digitalWrite(12, HIGH); //make sure relay and pi are off to start

  // Initialize serial communications
  Serial.begin(9600);   // Debug serial
  Serial1.begin(9600);  // Serial for Pi communication
  
  // Wait for serial monitor to open
  if (!Serial) delay(3000);
  
  Serial.println("Starting...");
  
  // Connect to WiFi
  connectToNetwork();
  
  // Make the clientID unique with MAC address
  byte mac[6];
  WiFi.macAddress(mac);
  for (int i = 0; i < 3; i++) {
    clientID += String(mac[i], HEX);
  }
  
  // Set MQTT credentials
  mqttClient.setId(clientID);
  mqttClient.setUsernamePassword(SECRET_MQTT_USER, SECRET_MQTT_PASS);
  
  // Clear any data in the serial buffer
  while (Serial1.available()) {
    Serial1.read();
  }
  
  Serial.println("Setup complete. Simple text protocol enabled.");
  publishStatus("Arduino initialized with text protocol. Waiting for Pi...");

  // Initialize text buffer
  memset(textBuffer, 0, sizeof(textBuffer));
  textBufferIndex = 0;
}

void loop() {
  // Check WiFi connection
  if (WiFi.status() != WL_CONNECTED) {
    connectToNetwork();
    return;
  }

  // Check MQTT broker connection
  if (!mqttClient.connected()) {
    Serial.println("Attempting to connect to broker");
    connectToBroker();
  }
  
  // Poll for new MQTT messages
  mqttClient.poll();
  
  // Check for Pi messages
  checkPiMessages();
  
  // Process any pending logic here
  
  delay(10);  // Small delay to prevent busy-wait
}

void checkPiMessages() {
  while (Serial1.available() > 0) {
    char inChar = Serial1.read();
    
    // Add to buffer if it's printable ASCII or newline/carriage return
    if ((inChar >= 32 && inChar <= 126) || inChar == 10 || inChar == 13) {
      if (textBufferIndex < sizeof(textBuffer) - 1) {
        textBuffer[textBufferIndex++] = inChar;
        
        // Process complete lines
        if (inChar == 10 || inChar == 13) { // Newline or carriage return
          textBuffer[textBufferIndex] = 0; // Null-terminate
          processTextMessage(textBuffer);
          
          // Reset buffer after processing
          memset(textBuffer, 0, sizeof(textBuffer));
          textBufferIndex = 0;
        }
      } else {
        // Text buffer overflow, reset
        Serial.println("Text buffer overflow, resetting");
        memset(textBuffer, 0, sizeof(textBuffer));
        textBufferIndex = 0;
      }
    }
  }
}

void processTextMessage(const char* message) {
  String messageStr = String(message);
  messageStr.trim(); // Remove leading/trailing whitespace
  
  Serial.print("Text message from Pi: ");
  Serial.println(messageStr);
  
  // Check for Pi ready message
  if (messageStr.indexOf("Pi ready") >= 0 || 
      messageStr.indexOf("READY") >= 0) {
    piReady = true;
    piPowered = true;
    Serial.println("Pi is now ready for communication!");
    publishStatus("Pi is ready for communication");
    
    // If we have a pending command, send it now
    if (hasPendingCommand) {
      Serial.println("Sending pending command to Pi");
      sendToPi(pendingCommand);
      hasPendingCommand = false;
      pendingCommand = "";
    }
    return;
  }
  
  // Process responses from Pi
  if (messageStr.startsWith("103_ACK")) {
    Serial.println("Received status acknowledgment from Pi");
    publishStatus("Pi status received: " + messageStr);
  } 
  else if (messageStr.startsWith("104_ACK")) {
    Serial.println("Received shutdown acknowledgment from Pi");
    publishStatus("Pi shutdown acknowledged: " + messageStr);
    
    // Reset Pi ready flag since it's shutting down
    piReady = false;
  } 
  else if (messageStr.startsWith("105_NOAA15") || 
           messageStr.startsWith("106_NOAA18") || 
           messageStr.startsWith("107_NOAA19") || 
           messageStr.startsWith("108_ISS")) {
    // Satellite recording acknowledgment
    Serial.println("Received satellite recording ACK: " + messageStr);
    publishStatus("Recording started: " + messageStr);
  } 
  else if (messageStr.startsWith("UPLOAD_SUCCESS")) {
    // Upload success message
    Serial.println("File upload success: " + messageStr);
    publishStatus("File uploaded to Google Drive: " + messageStr);
  } 
  else if (messageStr.startsWith("SHUTDOWN_INITIATED")) {
    // Pi is shutting down
    Serial.println("Pi shutdown initiated: " + messageStr);
    publishStatus("Pi shutting down: " + messageStr);
    piReady = false;
    delay(95000); //wait for ~1.5 minutes for pi to shut down - sudo shutdown -h + 1 (minute) - 
    //turn relay off once pi is off
    digitalWrite(12, HIGH);
    piPowered = false;
  } 
  else if (messageStr.startsWith("UNKNOWN_CODE")) {
    // Pi didn't recognize the command
    Serial.println("Pi reported unknown command: " + messageStr);
    publishStatus("Command not recognized by Pi: " + messageStr);
  }
  else {
    // Unknown response
    Serial.println("Unrecognized message from Pi: " + messageStr);
    publishStatus("Pi message: " + messageStr);
  }
}

void publishStatus(String message) {
  if (mqttClient.connected()) {
    mqttClient.beginMessage(topic);
    mqttClient.print(message);
    mqttClient.endMessage();
    Serial.println("Published: " + message);
  }
}

void connectToNetwork() {
  Serial.println("Connecting to WiFi...");
  while (WiFi.status() != WL_CONNECTED) {
    Serial.println("Attempting to connect to: " + String(SECRET_SSID));
    WiFi.begin(SECRET_SSID, SECRET_PASS);
    delay(5000);
  }

  Serial.print("Connected. IP address: ");
  Serial.println(WiFi.localIP());
  digitalWrite(LED_BUILTIN, HIGH);
}

boolean connectToBroker() {
  if (!mqttClient.connect(broker, port)) {
    Serial.print("MQTT connection failed. Error: ");
    Serial.println(mqttClient.connectError());
    return false;
  }

  mqttClient.onMessage(onMqttMessage);
  
  // Subscribe to the command topic for JSON messages
  String jsonTopic = "commands";
  Serial.print("Subscribing to topic: ");
  Serial.println(jsonTopic);
  mqttClient.subscribe(jsonTopic);
  
  // Also subscribe to the original topic
  Serial.print("Subscribing to topic: ");
  Serial.println(topic);
  mqttClient.subscribe(topic);
  
  // Send connection status
  publishStatus("Arduino connected to MQTT");
  
  return true;
}

void onMqttMessage(int messageSize) {
  // Message topic
  String messageTopic = mqttClient.messageTopic();
  
  // Read the message
  String incoming = "";
  while (mqttClient.available()) {
    incoming += (char)mqttClient.read();
  }
  
  Serial.print("MQTT received on topic ");
  Serial.print(messageTopic);
  Serial.print(": ");
  Serial.println(incoming);

  // Check if this is a JSON message
  if (incoming.indexOf("{") >= 0 && incoming.indexOf("}") >= 0) {
    // This looks like a JSON message, try to parse it
    processJsonMessage(incoming);
    return;
  }

  // Process legacy numeric codes
  int code = incoming.toInt();
  
  if (code == 101) {
    reply101();
  } 
  if (code == 102) {
    turnonpi();
  }
  
  // Process commands only if Pi is ready or special commands
  if (piReady) {
    if (code == 103) {
      statuspi();
    }
    else if (code == 104) {
      shutdownpi();
    }
    else if (code > 0) {
      // Forward code to Pi
      sendToPi(incoming);
    }
  } else if (code > 0 && code != 101 && code != 102) {
    // Pi is not ready but we received a command
    // Queue it for when Pi becomes ready
    pendingCommand = incoming;
    hasPendingCommand = true;
    
    // If Pi is not powered, turn it on
    if (!piPowered) {
      turnonpi();
      publishStatus("Pi booting, command " + incoming + " queued");
    } else {
      publishStatus("Pi not ready, command " + incoming + " queued");
    }
  }
}

void processJsonMessage(String jsonString) {
  // Allocate the JSON document
  StaticJsonDocument<512> doc;
  
  // Parse JSON object
  DeserializationError error = deserializeJson(doc, jsonString);
  
  // Test if parsing succeeds
  if (error) {
    Serial.print("JSON parsing failed: ");
    Serial.println(error.f_str());
    publishStatus("Error: JSON parsing failed");
    return;
  }
  
  // Extract values
  const char* command = doc["command"];
  int code = doc["code"];
  int duration = doc["duration_estimate"];
  
  // Check for valid values
  if (code <= 0) {
    Serial.println("Invalid code in JSON message");
    publishStatus("Error: Invalid code in JSON message");
    return;
  }
  
  Serial.print("JSON Command: ");
  Serial.println(command);
  Serial.print("Code: ");
  Serial.println(code);
  Serial.print("Duration: ");
  Serial.println(duration);
  
  // Create a command string with both code and duration
  String piCommand = String(code) + ":" + String(duration);
  
  // Log the command and duration
  Serial.print("Command with duration: ");
  Serial.println(piCommand);
  
  // Publish command information via MQTT
  publishStatus("Command " + String(code) + " with duration: " + String(duration) + " seconds");
  
  // Process special commands immediately
  if (code == 101) {
    reply101();
    return;
  }
  
  if (code == 102) {
    turnonpi();
    
    // If the command is power on, queue the real operation after boot
    if (strcmp(command, "power_on") == 0 && code != 102) {
      pendingCommand = piCommand;
      hasPendingCommand = true;
      publishStatus("Pi booting, command " + piCommand + " queued");
    }
    return;
  }
  
  // Send command to Pi if it's ready, otherwise queue it
  if (piReady) {
    sendToPi(piCommand);
  } else {
    // Handle the case when Pi is not ready
    pendingCommand = piCommand;
    hasPendingCommand = true;
    
    // If Pi is powered off, turn it on
    if (!piPowered) {
      turnonpi();
      publishStatus("Pi booting, command " + piCommand + " queued");
    } else {
      publishStatus("Pi not ready, command " + piCommand + " queued");
    }
  }
}

void reply101() {
  publishStatus("301");
  Serial.println("Status reported: 301");
}

void turnonpi() {
  // Only proceed if Pi is not already powered
  if (!piPowered) {
    Serial.println("Turning on Pi");
    digitalWrite(12, LOW);  // Turn on Pi
    publishStatus("Pi Booting");
    piPowered = true;
    piReady = false;  // Reset Pi ready flag
  } else {
    Serial.println("Pi is already powered on");
    publishStatus("Pi is already powered on");
  }
}

void statuspi() {
  Serial.println("Requesting Pi status");
  sendToPi("103");
}

void shutdownpi() {
  Serial.println("Sending shutdown command to Pi");
  sendToPi("104");
  
  // Reset Pi ready flag since it's shutting down
  piReady = false;
  publishStatus("Pi shutting down, communication disabled");
  
  // Note: Uncomment the following code if you want to power off the Pi after shutdown
  
  delay(25000);  // Adjust delay based on Pi shutdown time - 25 seconds - sudo shutdown now
  digitalWrite(12, HIGH);  // Turn off power to Pi
  piPowered = false;
  publishStatus("Pi power off");
  
}

void sendToPi(String command) {
  // Only send if Pi is ready
  if (!piReady) {
    Serial.println("Cannot send command, Pi not ready");
    publishStatus("Command failed, Pi not ready");
    
    // Queue the command for later if Pi is at least powered
    if (piPowered) {
      pendingCommand = command;
      hasPendingCommand = true;
      publishStatus("Command queued: " + command);
    }
    return;
  }
  
  // Add newline to the command for proper termination
  command += "\n";
  
  // Send the command to Pi
  Serial.print("Sending command to Pi: ");
  Serial.print(command);
  Serial1.print(command);
  Serial1.flush();  // Ensure transmission completes
  
  publishStatus("Command sent to Pi: " + command);
}