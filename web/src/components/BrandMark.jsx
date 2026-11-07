// PoopWatch logo: a friendly swirl with a pair of watchful eyes.
export default function BrandMark({ size = 26 }) {
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 64 64"
      role="img"
      aria-label="PoopWatch"
    >
      <ellipse cx="32" cy="46" rx="20" ry="11" fill="#8a5a2b" />
      <ellipse cx="32" cy="34" rx="15" ry="9.5" fill="#a86f36" />
      <ellipse cx="32" cy="23" rx="10" ry="7" fill="#c08542" />
      <circle cx="32" cy="14" r="4.5" fill="#c08542" />
      <ellipse cx="27" cy="22" rx="1.8" ry="2.2" fill="#161b21" />
      <ellipse cx="37" cy="22" rx="1.8" ry="2.2" fill="#161b21" />
      <path d="M27 29 Q32 32 37 29" stroke="#161b21" strokeWidth="1.6" fill="none" strokeLinecap="round" />
      <ellipse cx="24" cy="44" rx="4" ry="1.6" fill="#f5e6d0" opacity="0.35" />
    </svg>
  )
}
