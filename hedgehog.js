
/* ===========================
   ВИРТУАЛЬНЫЙ ЁЖИК
   =========================== */

(function () {
    const launcher = document.getElementById("hedgehog-launcher");
    const room = document.getElementById("hedgehog-room");
    const closeButton = document.getElementById("hedgehog-close");
    const micButton = document.getElementById("hedgehog-mic");
    const character = document.getElementById("hedgehog-character");
    const root = document.getElementById("hedgehog-root");

    let recognition = null;
    let activityTimer = null;
    let speaking = false;

    function currentApiKey() {
        return window.apiKey || localStorage.getItem("lenya_api_key") || "";
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
            return;
        }

        speaking = true;
        speechSynthesis.cancel();

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

        speechSynthesis.speak(utterance);
    }

    async function sendToHedgehog(text) {
        const key = currentApiKey();

        if (!key) {
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

            if (data.success && data.answer) {
                speak(data.answer);
            } else {
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

        if (speaking) {
            speechSynthesis.cancel();
            speaking = false;
        }

        recognition = new SpeechRecognition();
        recognition.lang = "ru-RU";
        recognition.interimResults = false;
        recognition.continuous = false;

        recognition.onstart = function () {
            micButton.classList.add("listening");
            setState("thinking");
        };

        recognition.onresult = function (event) {
            const text = event.results[0][0].transcript;
            sendToHedgehog(text);
        };

        recognition.onerror = function (event) {
            console.warn("Ошибка распознавания:", event.error);
            setState("idle");
        };

        recognition.onend = function () {
            micButton.classList.remove("listening");
        };

        recognition.start();
    }

    async function randomActivity() {
        if (!room.classList.contains("open") || speaking) return;

        const key = currentApiKey();
        if (!key) return;

        try {
            const response = await fetch("/hedgehog/activity", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ api_key: key })
            });

            const data = await response.json();

            if (data.success && data.action) {
                setState(data.action);

                if (data.action !== "idle") {
                    setTimeout(function () {
                        if (!speaking) setState("idle");
                    }, 5000);
                }
            }
        } catch (error) {
            console.warn("Не удалось получить действие ёжика:", error);
        }
    }

    function openRoom() {
        room.classList.add("open");
        setState("idle");

        clearInterval(activityTimer);
        activityTimer = setInterval(function () {
            // Действие выбирается не каждую секунду, а редко.
            if (Math.random() < 0.35) {
                randomActivity();
            }
        }, 12000);
    }

    function closeRoom() {
        room.classList.remove("open");
        clearInterval(activityTimer);
        speechSynthesis.cancel();
        speaking = false;
        setState("idle");
    }

    launcher.addEventListener("click", openRoom);
    closeButton.addEventListener("click", closeRoom);
    micButton.addEventListener("click", startRecognition);

    // Показываем кнопку, когда пользователь находится в основном чате.
    function updateLauncher() {
        const chatScreen = document.getElementById("chatScreen");
        if (!chatScreen) return;

        const visible = getComputedStyle(chatScreen).display !== "none";
        launcher.style.display = visible ? "block" : "none";
    }

    const observer = new MutationObserver(updateLauncher);
    const chatScreen = document.getElementById("chatScreen");

    if (chatScreen) {
        observer.observe(chatScreen, {
            attributes: true,
            attributeFilter: ["style", "class"]
        });
    }

    updateLauncher();
})();
