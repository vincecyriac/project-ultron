/**
 * Wake Engine for Project Ultron
 * Handles passive double-clap detection and local wake-word ("ULTRON") listening.
 */

class WakeEngineManager {
  constructor() {
    this.isActive = false;
    this.onWakeCallback = null;
    this.audioCtx = null;
    this.analyser = null;
    this.microphone = null;
    this.clapTimestamps = [];
    this.speechRecognition = null;
    
    // Clap detection parameters
    this.CLAP_THRESHOLD = 0.5; // Amplitude threshold (0 to 1)
    this.DOUBLE_CLAP_MIN_INTERVAL = 100; // ms
    this.DOUBLE_CLAP_MAX_INTERVAL = 500; // ms
  }

  /**
   * Initializes the wake engine. Must be called after a user interaction to satisfy
   * browser autoplay policies for AudioContext and SpeechRecognition.
   * @param {Function} onWake - Callback invoked when a wake event (double-clap or wake word) occurs.
   */
  async init(onWake) {
    if (this.isActive) return;
    this.onWakeCallback = onWake;

    try {
      await this.initAudioContext();
      this.initSpeechRecognition();
      this.isActive = true;
      console.log("[WakeEngine] Initialized and listening passively...");
    } catch (err) {
      console.error("[WakeEngine] Initialization failed:", err);
    }
  }

  async initAudioContext() {
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.microphone = this.audioCtx.createMediaStreamSource(stream);
    this.analyser = this.audioCtx.createAnalyser();
    
    this.analyser.fftSize = 256;
    this.analyser.smoothingTimeConstant = 0.2;
    this.microphone.connect(this.analyser);

    this.detectClaps();
  }

  detectClaps() {
    if (!this.isActive && this.audioCtx) return;

    const dataArray = new Uint8Array(this.analyser.frequencyBinCount);
    
    const analyze = () => {
      if (!this.isActive) return;
      
      this.analyser.getByteTimeDomainData(dataArray);
      
      let maxVol = 0;
      for (let i = 0; i < dataArray.length; i++) {
        // Normalize 0-255 to -1 to 1, then take absolute value
        const normalized = Math.abs((dataArray[i] / 128.0) - 1.0);
        if (normalized > maxVol) maxVol = normalized;
      }

      if (maxVol > this.CLAP_THRESHOLD) {
        const now = Date.now();
        // Debounce single claps
        if (this.clapTimestamps.length === 0 || (now - this.clapTimestamps[this.clapTimestamps.length - 1] > this.DOUBLE_CLAP_MIN_INTERVAL)) {
          this.clapTimestamps.push(now);
          this.checkDoubleClap();
        }
      }

      requestAnimationFrame(analyze);
    };

    analyze();
  }

  checkDoubleClap() {
    const now = Date.now();
    // Filter out old claps
    this.clapTimestamps = this.clapTimestamps.filter(t => now - t <= this.DOUBLE_CLAP_MAX_INTERVAL + 100);

    if (this.clapTimestamps.length >= 2) {
      const interval = this.clapTimestamps[1] - this.clapTimestamps[0];
      if (interval >= this.DOUBLE_CLAP_MIN_INTERVAL && interval <= this.DOUBLE_CLAP_MAX_INTERVAL) {
        console.log("[WakeEngine] Double-clap detected!");
        this.triggerWake();
        this.clapTimestamps = []; // Reset
      }
    }
  }

  initSpeechRecognition() {
    const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!SpeechRecognition) {
      console.warn("[WakeEngine] Web Speech API not supported in this browser.");
      return;
    }

    this.speechRecognition = new SpeechRecognition();
    this.speechRecognition.continuous = true;
    this.speechRecognition.interimResults = false;
    this.speechRecognition.lang = 'en-US';

    this.speechRecognition.onresult = (event) => {
      const latestResult = event.results[event.results.length - 1];
      if (latestResult.isFinal) {
        const transcript = latestResult[0].transcript.trim().toLowerCase();
        console.log(`[WakeEngine] Heard: "${transcript}"`);
        if (transcript.includes("ultron")) {
          console.log("[WakeEngine] Wake-word detected!");
          this.triggerWake();
        }
      }
    };

    this.speechRecognition.onend = () => {
      // Auto-restart for continuous listening
      if (this.isActive) {
        try {
          this.speechRecognition.start();
        } catch (e) {
          // Ignore "already started" errors
        }
      }
    };

    this.speechRecognition.start();
  }

  triggerWake() {
    // Request fullscreen
    if (!document.fullscreenElement) {
      document.documentElement.requestFullscreen().catch((err) => {
        console.warn(`[WakeEngine] Error attempting to enable full-screen mode: ${err.message}`);
      });
    }

    if (this.onWakeCallback) {
      this.onWakeCallback();
    }
  }

  stop() {
    this.isActive = false;
    if (this.audioCtx) {
      this.audioCtx.close();
      this.audioCtx = null;
    }
    if (this.speechRecognition) {
      this.speechRecognition.stop();
    }
  }
}

export const WakeEngine = new WakeEngineManager();
