/* ===========================
   ВИРТУАЛЬНЫЙ ЁЖИК — отдельная страница
   =========================== */

(function () {
    "use strict";

    const room = document.getElementById("hedgehog-room");
    const backButton = document.getElementById("hedgehog-back");
    const micButton = document.getElementById("hedgehog-mic");
    const character = document.getElementById("hedgehog-character");
    const root = document.getElementById("hedgehog-root");

    // Не запускаем код на странице, если нужных элементов нет.
    if (!room || !backButton || !micButton || !character || !root) {
        return;
    }

    let recognition = null;
    let activityTimer = null;
    let speaking = false;

    function currentApiKey() {
        return localStorage.getItem("lenya_api_key") || "";
    }

    function setState(state) {
        character.classList.remove(
            "thinking", "talking", "sleeping", "searching", "eating"
        );
        root.classList.remove("show");

        if (state && state !== "idle") {
            character.classList.add(state);
        }

        if (state === "eating") {
            root.classList.add("show");
        }
    }

    function speak(text) {
        if (!("speechSynthesis" in window)) {
            console.warn("В браузере нет синтеза речи.");
            setState("idle");
            return;
        }

        speaking = true;
        window.speechSynthesis.cancel();

        const utterance = new SpeechSynthesisUtterance(text);
        utterance.lang = "ru-RU";
        utterance.rate = 1.05;
        utterance.pitch = 1.18;

        setState("talking");

        utterance.onend = function () {
            speaking = false;
            setState("idle");
        };

        utterance.onerror = function () {
            speaking = false;
            setState("idle");
        };

        window.speechSynthesis.speak(utterance);
    }

    async function sendToHedgehog(text) {
        const key = currentApiKey();

        if (!key) {
            alert("Сначала войди в OmniLAMo, потом можно будет разговаривать с ёжиком.");
            setState("idle");
            return;
        }

        setState("thinking");

        try {
            const response = await fetch("/hedgehog", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    api_key: key,
                    text: text
                })
            });

            const data = await response.json();

            if (response.ok && data.success && data.answer) {
                speak(data.answer);
            } else {
                console.warn("Ёжик не ответил:", data);
                setState("idle");
            }
        } catch (error) {
            console.error("Ошибка разговора с ёжиком:", error);
            setState("idle");
        }
    }

    function startRecognition() {
        const SpeechRecognition =
            window.SpeechRecognition || window.webkitSpeechRecognition;

        if (!SpeechRecognition) {
            alert("В этом браузере не работает распознавание речи.");
            return;
        }

        if (recognition) {
            try {
                recognition.abort();
            } catch (error) {
                // Ничего страшного: создадим новый объект ниже.
            }
        }

        if (speaking) {
            window.speechSynthesis.cancel();
            speaking = false;
        }

        recognition = new SpeechRecognition();
        recognition.lang = "ru-RU";
        recognition.interimResults = false;
        recognition.continuous = false;
        recognition.maxAlternatives = 1;

        recognition.onstart = function () {
            micButton.classList.add("listening");
            setState("thinking");
        };

        recognition.onresult = function (event) {
            const text = event.results[0][0].transcript.trim();
            if (text) {
                sendToHedgehog(text);
            } else {
                setState("idle");
            }
        };

        recognition.onerror = function (event) {
            console.warn("Ошибка распознавания:", event.error);
            setState("idle");
        };

        recognition.onend = function () {
            micButton.classList.remove("listening");
            recognition = null;
        };

        try {
            recognition.start();
        } catch (error) {
            console.warn("Не удалось запустить микрофон:", error);
            micButton.classList.remove("listening");
            setState("idle");
        }
    }

    async function randomActivity() {
        if (speaking || recognition) return;

        const key = currentApiKey();
        if (!key) return;

        try {
            const response = await fetch("/hedgehog/activity", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ api_key: key })
            });

            const data = await response.json();

            if (response.ok && data.success && data.action) {
                setState(data.action);

                if (data.action !== "idle") {
                    window.setTimeout(function () {
                        if (!speaking && !recognition) {
                            setState("idle");
                        }
                    }, 5000);
                }
            }
        } catch (error) {
            console.warn("Не удалось получить действие ёжика:", error);
        }
    }

    function startActivityTimer() {
        window.clearInterval(activityTimer);

        activityTimer = window.setInterval(function () {
            // Редко выбираем действие, чтобы ёжик не суетился постоянно.
            if (Math.random() < 0.35) {
                randomActivity();
            }
        }, 12000);
    }

    function stopEverything() {
        window.clearInterval(activityTimer);

        if (recognition) {
            try {
                recognition.abort();
            } catch (error) {
                // Игнорируем ошибку остановки.
            }
            recognition = null;
        }

        if ("speechSynthesis" in window) {
            window.speechSynthesis.cancel();
        }

        speaking = false;
        setState("idle");
    }

    backButton.addEventListener("click", function () {
        stopEverything();
        window.location.href = "/index.html";
    });

    micButton.addEventListener("click", startRecognition);

    window.addEventListener("beforeunload", stopEverything);

    setState("idle");
    startActivityTimer();
})();
