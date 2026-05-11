Please profile the compiler and optimize the hot-spots to improve its performance.

1. Review the `git log -p main..head` to see any recent changes. Run a compile
   instrumenting the JVM both with JFR (1ms sample period) and `-Vprofile` so you can get a
   performance profile you can analyze. If the profile is not clean, do whatever it takes to get a
   clean profile that clearly illustrates where time is being spent during compilation. Analyze
   the performance profile and cross-reference it to the scala3 codebase to try and find hotspots:
   whether bottlenecks at the bottom of the call stack, algorithmically inefficient code further
   up the callsite, or data structures that could be improved.

2. Spawn five extra-high-effort sub-agents to analyze the JFR profiles and try to find 
   opportunities for optimization. Based on JFR top-down and bottom-up call tree profiles and
   -Vprofile and -Ystats data, 
   come up with *high-level algorithm, data structure, or architectural improvements*
   that would substantially improve the performance of the compiler, rather than minor micro-optimizations
   or nitpicks. For each agent that reports optimization opportunities, spawn a second agent to
   deeply investigate the proposed optimizations and how they fit into the relevant parts of the
   codebase and performance profiles to verify if they are legitimate and satisfy the requirements 
   above.

3. Pick the issues that are most likely to substantially improve the performance of the `~/Github/mill`
   compilation given your profile analysis - potentially more than one - and implement them.
   Anything from micro-optimizations to broader cross-cutting changes can be considered.
   Even a fraction of a % improvement can be valuable as they add up over time.
   Feel free to add your own call counters if necessary and run more clean compiles to confirm
   hypotheses about hot spots and how often they are called. Re-run the JFR
   profiler after making the change and see if the expected drop in the time taken for that
   particular method actually happens

4. Run basic smoke-tests to make sure your change doesn't break anything. If
   anything breaks, have the sub-agent report the breakage. As a smoketest, compile
   the bootstrapped compiler and standard library. If it succeeds, make a `git commit`
   with an explanation for what the most recent `git diff` change is mean to accomplish, or just
   "first commit" if it's the first time doing this, along with the top-line number for how long
   it took to clean compile *after* your change. Summarize your most important findings
   and anything else it discovered that may come in useful in future in a as part of the commit
   message. Even if the change is not sucessful at improving performance, revert the code change
   and make an empty commit with your findings. Push the commit to
   https://github.com/scala/scala3/pull/26025 using `git push origin head`

DO NOT PERFORM ANY GIT OPERATIONS OTHER THAN THE ONES LISTED ABOVE

Also please maintain the `bench-mill-javalib/` folder with any useful scripts you need so future 
iterations can use them conveniently, and place any ephemeral output files or reports in
`target/bench-mill-javalib/`.
