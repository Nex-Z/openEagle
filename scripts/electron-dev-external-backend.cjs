process.env.OPEN_EAGLE_BACKEND_HOST ||= "127.0.0.1";
process.env.OPEN_EAGLE_BACKEND_PORT ||= "8765";

require("./electron-dev.cjs");
