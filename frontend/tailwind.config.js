/** Tailwind build config for index.html (control plane) and pay.html (checkout simulator). */
const accents = ['indigo', 'cyan', 'amber', 'coral', 'green'];

// Classes assembled at runtime in views/layout_bottom.html (e.g. `bg-accent-${color}/20`)
// are invisible to the content scanner, so they are safelisted explicitly.
const dynamic = accents.flatMap((c) => [
  `bg-accent-${c}/20`,
  `text-accent-${c}`,
  `border-accent-${c}/30`,
]);

module.exports = {
  darkMode: 'class',
  content: ['./index.html', './pay.html', './views/*.html', './js/*.js'],
  safelist: dynamic,
  theme: {
    extend: {
      colors: {
        'bg-carbon': '#070708',
        'surface-card': '#101112',
        'surface-elevated': '#151618',
        'surface-sidebar': '#0d0e0f',
        'border-hairline': '#232426',
        'border-light': '#2e3034',
        'text-main': '#e5e2e3',
        'text-muted': '#8f9299',
        'text-subtle': '#585a60',
        'accent-indigo': '#5E6BFF',
        'accent-cyan': '#50d8e9',
        'accent-amber': '#e0731d',
        'accent-coral': '#ff6b6b',
        'accent-green': '#22c55e',
      },
      fontFamily: {
        heading: ['Manrope', 'sans-serif'],
        body: ['Inter', 'sans-serif'],
        mono: ['JetBrains Mono', 'monospace'],
      },
    },
  },
  plugins: [require('@tailwindcss/forms')],
};
