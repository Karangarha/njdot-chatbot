import Image from 'next/image'
import Link from 'next/link'

export default function Home() {
  return (
    <div className="flex min-h-screen flex-col">

      {/* ── NAVBAR ────────────────────────────────────────────────────────────── */}
      <nav
        className="flex items-center justify-between px-6 py-3"
        style={{ background: '#1B3A6B' }}
      >
        <div className="flex items-center gap-3">
          <Image
            src="/njdot_logo.png"
            alt="NJDOT"
            width={36}
            height={36}
            priority
          />
          <span className="text-sm font-semibold text-white">
            Department of Transportation
          </span>
        </div>
        <Link
          href="/login"
          className="rounded-md px-4 py-1.5 text-sm font-semibold text-white/90 transition-colors hover:bg-white/10"
        >
          Sign In
        </Link>
      </nav>

      {/* ── HERO ──────────────────────────────────────────────────────────────── */}
      <section className="relative flex min-h-[500px] items-center justify-center overflow-hidden">
        {/* Background photo */}
        <Image
          src="/highway.jpg"
          alt="New Jersey highway aerial view"
          fill
          style={{ objectFit: 'cover', objectPosition: 'center' }}
          priority
        />
        {/* Dark overlay */}
        <div
          className="absolute inset-0"
          style={{
            background:
              'linear-gradient(135deg, rgba(15,30,60,0.85) 0%, rgba(10,20,45,0.80) 100%)',
          }}
        />
        {/* Content */}
        <div className="relative z-10 flex flex-col items-center px-6 py-16 text-center">
          <h1 className="mb-3 text-4xl font-bold tracking-tight text-white sm:text-5xl">
            Construction Management Smart Assistant  
          </h1>
          <p className="mb-2 text-lg font-medium text-white/80">
            Modernizing NJDOT Workflows with AI
          </p>
          <p className="mb-10 max-w-xl text-sm leading-relaxed text-white/65">
            AI-powered assistant and document review system for NJDOT workflows.
            Ask questions and get answers grounded in official specifications,
            manuals, and material procedures.
          </p>

          <div className="flex flex-col items-center gap-3 sm:flex-row">
            <Link
              href="/signup"
              className="w-44 rounded-lg bg-[#CC2529] px-8 py-3 text-center text-sm font-semibold text-white shadow-lg transition-colors hover:bg-[#a81e21] focus:outline-none focus:ring-2 focus:ring-[#CC2529]/40"
            >
              Get Started
            </Link>
            <Link
              href="/login"
              className="w-44 rounded-lg border border-white/40 bg-white/10 px-8 py-3 text-center text-sm font-semibold text-white backdrop-blur-sm transition-colors hover:bg-white/20 focus:outline-none focus:ring-2 focus:ring-white/30"
            >
              Try Assistant
            </Link>
          </div>
        </div>
      </section>

      {/* ── PHOTO BREAK ───────────────────────────────────────────────────────── */}
      <section className="relative flex h-[220px] items-center justify-center overflow-hidden">
        <Image
          src="/workers_pic.jpg"
          alt="NJDOT workers installing road sign"
          fill
          style={{ objectFit: 'cover', objectPosition: 'center' }}
        />
        <div
          className="absolute inset-0"
          style={{ background: 'rgba(0,0,0,0.45)' }}
        />
        <div className="relative z-10 px-6 text-center">
          <p className="text-xl font-bold text-white sm:text-2xl">
            Built for NJDOT Field and Office Staff
          </p>
          <p className="mt-2 max-w-md text-sm text-white/75">
            Giving every team member instant access to official specifications
            and procedures
          </p>
        </div>
      </section>

      {/* ── FEATURES ──────────────────────────────────────────────────────────── */}
      <section className="bg-[#F5F5F5] px-6 py-16">
        <h2 className="mb-10 text-center text-2xl font-bold text-[#1B3A6B]">
          What can it do?
        </h2>
        <div className="mx-auto grid max-w-3xl gap-6 sm:grid-cols-2">

          {/* Card 1 — AI Assistant */}
          <div className="rounded-xl bg-white p-8 shadow-sm ring-1 ring-black/5">
            {/* MessageSquare icon */}
            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-red-50">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#CC2529"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z" />
              </svg>
            </div>
            <h3 className="mb-2 text-base font-bold text-[#1B3A6B]">
              AI Assistant
            </h3>
            <p className="text-sm leading-relaxed text-gray-500">
              Ask questions and get instant guidance on NJDOT standards and
              processes
            </p>
          </div>

          {/* Card 2 — Document Review */}
          <div className="rounded-xl bg-white p-8 shadow-sm ring-1 ring-black/5">
            {/* FileSearch icon */}
            <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-lg bg-red-50">
              <svg
                xmlns="http://www.w3.org/2000/svg"
                width="24"
                height="24"
                viewBox="0 0 24 24"
                fill="none"
                stroke="#CC2529"
                strokeWidth="2"
                strokeLinecap="round"
                strokeLinejoin="round"
              >
                <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" />
                <polyline points="14 2 14 8 20 8" />
                <circle cx="11" cy="15" r="2" />
                <line x1="13" y1="17" x2="16" y2="20" />
              </svg>
            </div>
            <h3 className="mb-2 text-base font-bold text-[#1B3A6B]">
              Document Review
            </h3>
            <p className="text-sm leading-relaxed text-gray-500">
              Upload plans and reports to detect issues and compliance gaps
            </p>
          </div>

        </div>
      </section>

      {/* ── FOOTER ────────────────────────────────────────────────────────────── */}
      <footer className="mt-auto px-6 py-5" style={{ background: '#1B3A6B' }}>
        <div className="mx-auto flex max-w-5xl flex-col items-center gap-3 sm:flex-row sm:gap-0">

          {/* Left: logos */}
          <div className="flex items-center gap-4 sm:flex-1">
            <div className="rounded-md bg-white px-3 py-1.5">
              <Image
                src="/njdot_long_logo.png"
                alt="NJDOT"
                width={110}
                height={36}
                style={{ height: '36px', width: 'auto' }}
              />
            </div>
            <div className="rounded-full bg-white p-1.5">
              <Image
                src="/rutgers.png"
                alt="Rutgers University"
                width={36}
                height={36}
                style={{ height: '36px', width: '36px', objectFit: 'contain' }}
              />
            </div>
            <div className="rounded-full bg-white p-1.5">
              <Image
                src="/Kean_smallLogo.png"
                alt="Kean University"
                width={36}
                height={36}
                style={{ height: '36px', width: '36px', objectFit: 'contain' }}
              />
            </div>
            <div className="rounded-full bg-white p-1.5">
              <Image
                src="/john-hopkin.png"
                alt="Johns Hopkins University"
                width={36}
                height={36}
                style={{ height: '36px', width: '36px', objectFit: 'contain' }}
              />
            </div>
          </div>

          {/* Center: copyright */}
          <p className="text-xs text-white/50 sm:flex-1 sm:text-center">
            &copy; 2026 NJDOT &middot; Kean University
          </p>

          {/* Right: contact */}
          <div className="flex flex-col items-center gap-0.5 sm:flex-1 sm:items-end">
            <a
              href="mailto:dan.liu@kean.edu"
              className="text-xs font-semibold text-white/80 underline-offset-2 hover:underline"
            >
              Contact Us
            </a>
            <p className="text-[11px] text-white/40">
              Have suggestions? We&apos;d love to hear from you.
            </p>
          </div>

        </div>
      </footer>

    </div>
  )
}
