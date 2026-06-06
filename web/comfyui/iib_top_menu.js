import { app } from "../../scripts/app.js";

const BUTTON_TOOLTIP = "打开 Infinite Image Browsing（图片浏览器）";
const IIB_PATH = "/iib";
const BUTTON_GROUP_CLASS = "iib-top-menu-group";
const MAX_ATTACH_ATTEMPTS = 120;
const MIN_VERSION_FOR_ACTION_BAR = [1, 33, 9];
const NEW_WINDOW_FEATURES = "width=1400,height=900,resizable=yes,scrollbars=yes,status=yes";

const getTargetUrl = () => `${window.location.origin}${IIB_PATH}`;

const openIIB = (event = {}) => {
    const url = getTargetUrl();
    if (event.shiftKey) {
        window.open(url, "_blank", NEW_WINDOW_FEATURES);
        return;
    }
    window.open(url, "_blank");
};

const getComfyUIFrontendVersion = async () => {
    try {
        if (window.__COMFYUI_FRONTEND_VERSION__) {
            return window.__COMFYUI_FRONTEND_VERSION__;
        }
    } catch (error) {
        console.warn("Infinite Image Browsing: unable to read frontend version:", error);
    }

    try {
        const response = await fetch("/system_stats");
        const data = await response.json();
        return data?.system?.comfyui_frontend_version || data?.system?.required_frontend_version || "0.0.0";
    } catch (error) {
        console.warn("Infinite Image Browsing: unable to fetch system_stats:", error);
    }

    return "0.0.0";
};

const parseVersion = (versionStr) => {
    if (!versionStr || typeof versionStr !== "string") return [0, 0, 0];
    const parts = versionStr.replace(/^[vV]/, "").split("-")[0].split(".").map((part) => parseInt(part, 10) || 0);
    while (parts.length < 3) parts.push(0);
    return parts;
};

const compareVersions = (version1, version2) => {
    const v1 = typeof version1 === "string" ? parseVersion(version1) : version1;
    const v2 = typeof version2 === "string" ? parseVersion(version2) : version2;
    for (let i = 0; i < 3; i++) {
        if (v1[i] > v2[i]) return 1;
        if (v1[i] < v2[i]) return -1;
    }
    return 0;
};

const supportsActionBarButtons = async () => {
    const version = await getComfyUIFrontendVersion();
    return compareVersions(version, MIN_VERSION_FOR_ACTION_BAR) >= 0;
};

const getIIBIcon = () => `
    <svg viewBox="0 0 24 24" fill="none" xmlns="http://www.w3.org/2000/svg" aria-hidden="true">
        <rect x="3" y="4" width="18" height="16" rx="2" stroke="currentColor" stroke-width="2"/>
        <circle cx="8" cy="9" r="2" fill="currentColor"/>
        <path d="M4.5 18L10 12.5L13.5 16L16 13.5L20 17.5" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>
    </svg>
`;

const injectStyles = () => {
    const styleId = "iib-top-menu-button-styles";
    if (document.getElementById(styleId)) return;

    const style = document.createElement("style");
    style.id = styleId;
    style.textContent = `
        .iib-top-menu-button svg,
        .${BUTTON_GROUP_CLASS} svg {
            width: 20px;
            height: 20px;
        }
        .iib-top-menu-button {
            transition: all 0.2s ease;
        }
    `;
    document.head.appendChild(style);
};

const createTopMenuButton = async () => {
    const { ComfyButton } = await import("../../scripts/ui/components/button.js");

    const button = new ComfyButton({
        icon: "iib",
        tooltip: BUTTON_TOOLTIP,
        app,
        enabled: true,
        classList: "comfyui-button comfyui-menu-mobile-collapse primary iib-top-menu-button",
    });

    button.element.setAttribute("aria-label", BUTTON_TOOLTIP);
    button.element.title = BUTTON_TOOLTIP;

    if (button.iconElement) {
        button.iconElement.innerHTML = getIIBIcon();
    } else {
        button.element.innerHTML = getIIBIcon();
    }

    button.element.addEventListener("click", openIIB);
    return button;
};

const attachLegacyTopMenuButton = async (attempt = 0) => {
    if (document.querySelector(`.${BUTTON_GROUP_CLASS}`)) return;

    const settingsGroup = app.menu?.settingsGroup;
    if (!settingsGroup?.element?.parentElement) {
        if (attempt >= MAX_ATTACH_ATTEMPTS) {
            console.warn("Infinite Image Browsing: unable to locate the ComfyUI settings button group.");
            return;
        }
        requestAnimationFrame(() => attachLegacyTopMenuButton(attempt + 1));
        return;
    }

    const button = await createTopMenuButton();
    const { ComfyButtonGroup } = await import("../../scripts/ui/components/buttonGroup.js");
    const buttonGroup = new ComfyButtonGroup(button);
    buttonGroup.element.classList.add(BUTTON_GROUP_CLASS);
    settingsGroup.element.before(buttonGroup.element);
};

const replaceActionBarButtonIcon = () => {
    const buttons = document.querySelectorAll(`button[aria-label="${BUTTON_TOOLTIP}"]`);
    buttons.forEach((button) => {
        button.classList.add("iib-top-menu-button");
        button.innerHTML = getIIBIcon();
        button.title = BUTTON_TOOLTIP;
    });
    if (buttons.length === 0) requestAnimationFrame(replaceActionBarButtonIcon);
};

const createExtensionObject = (useActionBar) => {
    const extensionObj = {
        name: "InfiniteImageBrowsing.TopMenu",
        async setup() {
            injectStyles();
            if (!useActionBar) {
                await attachLegacyTopMenuButton();
            } else {
                requestAnimationFrame(replaceActionBarButtonIcon);
            }
            this.aboutPageBadges = [
                {
                    label: "Infinite Image Browsing",
                    url: "https://github.com/zanllp/sd-webui-infinite-image-browsing",
                    icon: "pi pi-images",
                },
            ];
        },
    };

    if (useActionBar) {
        extensionObj.actionBarButtons = [
            {
                icon: "icon-[mdi--image-multiple] size-4",
                tooltip: BUTTON_TOOLTIP,
                onClick: openIIB,
            },
        ];
    }

    return extensionObj;
};

(async () => {
    const useActionBar = await supportsActionBarButtons();
    app.registerExtension(createExtensionObject(useActionBar));
})();
