import os
import cv2
import pyautogui
from PIL import Image
import io

def capture_screen(output_size=(768, 768)):
    """
    Captures the primary desktop screen using PyAutoGUI.
    Resizes it to output_size, encodes as JPEG, and returns raw bytes.
    """
    try:
        # Take screenshot
        screenshot = pyautogui.screenshot()
        # Convert RGBA to RGB (macOS Retina displays capture screenshots in RGBA)
        if screenshot.mode in ('RGBA', 'LA') or (screenshot.mode == 'P' and 'transparency' in screenshot.info):
            screenshot = screenshot.convert('RGB')
        # Resize to target resolution
        screenshot = screenshot.resize(output_size, Image.Resampling.LANCZOS)
        # Save to buffer as JPEG
        buffer = io.BytesIO()
        screenshot.save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()
    except Exception as e:
        return None

def capture_webcam(output_size=(768, 768)):
    """
    Captures a frame from the default webcam using OpenCV.
    Resizes it to output_size, encodes as JPEG, and returns raw bytes.
    """
    cap = None
    try:
        # 0 is usually the default webcam (e.g. MacBook FaceTime HD Camera)
        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None
        
        # Warm up the camera (read a few frames for auto-exposure)
        for _ in range(5):
            cap.read()
            
        ret, frame = cap.read()
        if not ret:
            return None
            
        # Convert BGR (OpenCV default) to RGB
        rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        
        # Convert to PIL Image for easy resizing
        image = Image.fromarray(rgb_frame)
        image = image.resize(output_size, Image.Resampling.LANCZOS)
        
        # Save to buffer as JPEG
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=80)
        return buffer.getvalue()
    except Exception as e:
        return None
    finally:
        if cap is not None:
            cap.release()

class PersistentWebcam:
    def __init__(self):
        self.cap = None
        
    def start(self):
        if self.cap is None or not self.cap.isOpened():
            self.cap = cv2.VideoCapture(0)
            if self.cap.isOpened():
                # Warm up the camera
                for _ in range(5):
                    self.cap.read()
            else:
                self.cap = None
                
    def read_frame(self, output_size=(768, 768)):
        if self.cap is None or not self.cap.isOpened():
            self.start()
        if self.cap is None:
            return None
        try:
            ret, frame = self.cap.read()
            if not ret:
                return None
            rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            image = Image.fromarray(rgb_frame)
            image = image.resize(output_size, Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=80)
            return buffer.getvalue()
        except Exception as e:
            return None
            
    def stop(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

if __name__ == "__main__":
    # Quick standalone sanity test
    print("Testing screen capture...")
    screen_data = capture_screen()
    if screen_data:
        print(f"Captured screen successfully! Size: {len(screen_data)} bytes.")
    else:
        print("Screen capture failed.")
        
    print("Testing persistent webcam capture...")
    webcam = PersistentWebcam()
    webcam.start()
    webcam_data = webcam.read_frame()
    if webcam_data:
        print(f"Captured webcam frame successfully! Size: {len(webcam_data)} bytes.")
    else:
        print("Webcam capture failed.")
    webcam.stop()
