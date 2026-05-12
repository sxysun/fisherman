import "./styles.css";

// The previous build had a falling-rain canvas + dot pattern that
// competed with content. This page is intentionally static — the
// only motion is :hover. Reduced-motion users are already covered
// by the CSS media query in styles.css.
//
// Keeping this file as the Vite entry so `index.html` doesn't need
// to change if we later add nav scroll-spy or a copy-install button.
