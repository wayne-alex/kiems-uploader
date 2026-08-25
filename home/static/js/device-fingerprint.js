class DeviceFingerprint {
    constructor() {
        this.fingerprint = null;
        this.burnData = null;
        this.initialized = false;
    }

    async initialize() {
        if (this.initialized) return this;

        // Generate or retrieve fingerprint
        this.fingerprint = await this.generateFingerprint();
        this.burnData = this.getBurnData();
        this.initialized = true;
        return this;
    }

    async generateFingerprint() {
        // Check if we have a stored fingerprint
        let fingerprint = localStorage.getItem('device_fingerprint');
        if (fingerprint) {
            // Verify the fingerprint is still valid
            const isValid = await this.verifyFingerprint(fingerprint);
            if (isValid) return fingerprint;
        }

        // Generate new fingerprint
        fingerprint = await this.createFingerprint();
        localStorage.setItem('device_fingerprint', fingerprint);
        return fingerprint;
    }

    async createFingerprint() {
        // Collect device data
        const data = {
            userAgent: navigator.userAgent,
            screenWidth: screen.width,
            screenHeight: screen.height,
            colorDepth: screen.colorDepth,
            language: navigator.language,
            platform: navigator.platform,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            canvasFingerprint: this.getCanvasFingerprint(),
            webglFingerprint: this.getWebGLFingerprint(),
            audioFingerprint: await this.getAudioFingerprint()
        };

        // Create hash
        const str = JSON.stringify(data);
        const encoder = new TextEncoder();
        const dataBuffer = encoder.encode(str);
        const hashBuffer = await crypto.subtle.digest('SHA-256', dataBuffer);
        const hashArray = Array.from(new Uint8Array(hashBuffer));
        return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
    }

    getCanvasFingerprint() {
        try {
            const canvas = document.createElement('canvas');
            canvas.width = 200;
            canvas.height = 50;
            const ctx = canvas.getContext('2d');
            ctx.textBaseline = 'top';
            ctx.font = '14px Arial';
            ctx.fillStyle = '#f60';
            ctx.fillRect(125, 1, 62, 20);
            ctx.fillStyle = '#069';
            ctx.fillText('KIEMS Fingerprint', 2, 15);
            ctx.fillStyle = 'rgba(102, 204, 0, 0.7)';
            ctx.fillText('IEBC', 4, 17);
            return canvas.toDataURL();
        } catch(e) {
            return 'canvas_error';
        }
    }

    getWebGLFingerprint() {
        try {
            const canvas = document.createElement('canvas');
            const gl = canvas.getContext('webgl') || canvas.getContext('experimental-webgl');
            if (!gl) return 'webgl_not_supported';

            const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
            if (!debugInfo) return 'webgl_debug_not_available';

            const vendor = gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL);
            const renderer = gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL);

            return `${vendor}_${renderer}`;
        } catch(e) {
            return 'webgl_error';
        }
    }

    async getAudioFingerprint() {
        try {
            const audioContext = new (window.AudioContext || window.webkitAudioContext)();
            const oscillator = audioContext.createOscillator();
            const analyser = audioContext.createAnalyser();
            oscillator.connect(analyser);
            analyser.connect(audioContext.destination);
            oscillator.frequency.value = 1000;
            oscillator.start(0);

            const dataArray = new Float32Array(analyser.fftSize);
            analyser.getFloatTimeDomainData(dataArray);

            oscillator.stop(0);
            audioContext.close();

            // Create a hash from the audio data
            const encoder = new TextEncoder();
            const dataBuffer = encoder.encode(JSON.stringify(dataArray));
            const hashBuffer = await crypto.subtle.digest('SHA-256', dataBuffer);
            const hashArray = Array.from(new Uint8Array(hashBuffer));
            return hashArray.map(b => b.toString(16).padStart(2, '0')).join('');
        } catch(e) {
            return 'audio_error';
        }
    }

    async verifyFingerprint(storedFingerprint) {
        // Regenerate fingerprint and compare
        const newFingerprint = await this.createFingerprint();
        return storedFingerprint === newFingerprint;
    }

    getBurnData() {
        try {
            const data = localStorage.getItem('device_burn_data');
            return data ? JSON.parse(data) : null;
        } catch {
            return null;
        }
    }

    setBurnData(data) {
        localStorage.setItem('device_burn_data', JSON.stringify(data));
        this.burnData = data;
    }

    clearBurnData() {
        localStorage.removeItem('device_burn_data');
        this.burnData = null;
    }

    async getDeviceInfo() {
        await this.initialize();
        return {
            fingerprint: this.fingerprint,
            userAgent: navigator.userAgent,
            screenResolution: `${screen.width}x${screen.height}`,
            colorDepth: screen.colorDepth,
            language: navigator.language,
            platform: navigator.platform,
            timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            burnData: this.burnData
        };
    }
}

// Initialize device fingerprint on page load
const deviceFingerprint = new DeviceFingerprint();

// Export for use in other scripts
window.DeviceFingerprint = DeviceFingerprint;
window.deviceFingerprint = deviceFingerprint;