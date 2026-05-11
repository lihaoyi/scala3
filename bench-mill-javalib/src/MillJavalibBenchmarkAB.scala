package dotty.tools.benchmarks.milljavalib

import java.io.File
import java.net.{URL, URLClassLoader}
import java.nio.file.{Files, Path, Paths}
import java.util.concurrent.TimeUnit
import scala.compiletime.uninitialized

import org.openjdk.jmh.annotations._

/**
 * A/B JMH benchmark for paired comparison of two locally-built scala3-compiler
 * snapshots (e.g. main vs autoperf). Loads each compiler in its OWN
 * `URLClassLoader`, dispatched by `@Param("baseline", "postchange")`.
 *
 * NB: this benchmark does NOT statically reference any dotty type. All
 * compiler invocation goes through reflection so we do not tie the
 * benchmark's defining classloader to one specific compiler build. The
 * paired path is intentionally independent of `MillJavalibBenchmark` (which
 * exists for single-compiler runs).
 *
 * Per-trial setup uses the file `<bench.dir>/target/build/compilers/<param>.classpath`
 * (one path per line, ":"-separated on disk if needed) which is prepared by
 * run-bench-ab.sh. The classpath snapshots live under
 * `target/bench-mill-javalib/target/build/compilers/{baseline,postchange}/`
 * and survive branch switches because the whole `target/` tree is gitignored.
 *
 * The compile workload (sources, classpath, scalac options) mirrors
 * `MillJavalibBenchmark` exactly so the absolute numbers are comparable.
 */
@State(Scope.Benchmark)
@BenchmarkMode(Array(Mode.AverageTime))
@OutputTimeUnit(TimeUnit.MILLISECONDS)
class MillJavalibBenchmarkAB {

  @Param(Array("baseline", "postchange"))
  var compiler: String = uninitialized

  // Filled in @Setup
  private var compilerLoader: URLClassLoader = uninitialized
  // Reflective handle on dotty.tools.dotc.Driver loaded from `compilerLoader`.
  // We instantiate ONCE and re-use across iterations (same as MillJavalibBenchmark).
  private var driver: AnyRef = uninitialized
  // Reflective method handle on Driver.process(Array[String], Reporter): Reporter.
  private var processMethod: java.lang.reflect.Method = uninitialized
  // StoreReporter class + constructor handle, loaded from compilerLoader.
  private var storeReporterCtor: java.lang.reflect.Constructor[?] = uninitialized

  private var baseArgs: Array[String] = uninitialized
  private var sourceFiles: Array[String] = uninitialized

  // Per-iteration scratch
  private var outDir: Path = uninitialized

  private def readLines(p: Path): List[String] = {
    val it = Files.lines(p)
    try {
      val b = List.newBuilder[String]
      it.forEach(s => if (s.trim.nonEmpty) b += s.trim)
      b.result()
    } finally it.close()
  }

  private def loadCompilerClasspath(benchDir: Path, key: String): Array[URL] = {
    val cpFile = benchDir.resolve("target/build/compilers").resolve(s"$key.classpath")
    if (!Files.exists(cpFile))
      throw new RuntimeException(s"[bench-ab] missing compiler classpath: $cpFile " +
        "(did you run run-bench-ab.sh to snapshot both compilers?)")
    val raw = readLines(cpFile)
    // Each line may itself be a `:`-separated chain or a single path.
    raw.flatMap(_.split(File.pathSeparator).toList)
       .filter(_.nonEmpty)
       .map { s =>
         val f = new File(s)
         if (!f.exists()) throw new RuntimeException(s"[bench-ab] classpath entry missing on disk: $s (compiler=$key)")
         f.toURI.toURL
       }
       .toArray
  }

  @Setup(Level.Trial)
  def setupTrial(): Unit = {
    val benchDir = Paths.get(
      Option(System.getProperty("bench.dir")).getOrElse("/Users/lihaoyi/Github/scala3/target/bench-mill-javalib")
    )
    val inputs = benchDir.resolve("inputs")
    val sources = benchDir.resolve("sources")

    val cp = readLines(inputs.resolve("compile-classpath.txt"))
    val opts = readLines(inputs.resolve("scalac-options.txt"))
    val srcsRel = readLines(inputs.resolve("source-files.txt"))

    sourceFiles = srcsRel.map(rel => sources.resolve(rel).toAbsolutePath.toString).toArray
    val cpStr = cp.mkString(File.pathSeparator)
    val argsBuf = scala.collection.mutable.ArrayBuffer[String]()
    argsBuf += "-classpath"
    argsBuf += cpStr
    argsBuf ++= opts
    baseArgs = argsBuf.toArray

    // Build an isolated classloader for the selected compiler.
    // Parent = platform loader (the bootstrap + JDK extension classes only).
    // We do NOT chain to the app classloader because the app classloader has
    // the OTHER compiler on it (the one the benchmark was compiled against)
    // and we want strict isolation.
    val urls = loadCompilerClasspath(benchDir, compiler)
    val parent = ClassLoader.getPlatformClassLoader
    compilerLoader = new URLClassLoader(urls, parent)

    // Use dotty.tools.dotc.Main (which extends Driver) reflectively so the
    // A/B harness only needs upstream compiler jars and no per-branch helper.
    val driverCls = Class.forName("dotty.tools.dotc.Main", true, compilerLoader)
    driver = driverCls.getDeclaredConstructor().newInstance().asInstanceOf[AnyRef]
    val reporterCls = Class.forName("dotty.tools.dotc.reporting.Reporter", true, compilerLoader)
    processMethod = driverCls.getMethod("process", classOf[Array[String]], reporterCls)

    val storeReporterCls = Class.forName("dotty.tools.dotc.reporting.StoreReporter", true, compilerLoader)
    // StoreReporter has multiple constructors; we use (Reporter) one. The
    // contract is: pass null to mean "no outer reporter to forward to".
    storeReporterCtor = storeReporterCls.getConstructor(reporterCls)

    val pluginCount = opts.count(_.startsWith("-Xplugin:"))
    System.out.println(s"[bench-ab] benchDir       = $benchDir")
    System.out.println(s"[bench-ab] compiler       = $compiler (${urls.length} cp entries)")
    System.out.println(s"[bench-ab] sources        = ${sourceFiles.length} files")
    System.out.println(s"[bench-ab] compile cp     = ${cp.size} jars")
    System.out.println(s"[bench-ab] scalac opts    = ${opts.size} (with $pluginCount -Xplugin entries)")
  }

  @Setup(Level.Iteration)
  def setupIteration(): Unit = {
    outDir = Files.createTempDirectory("mill-javalib-bench-ab-out-")
  }

  @TearDown(Level.Iteration)
  def teardownIteration(): Unit = {
    if (outDir != null) {
      import scala.jdk.CollectionConverters._
      val walk = Files.walk(outDir)
      try {
        val all = walk.iterator().asScala.toList.reverse
        all.foreach { p => try Files.deleteIfExists(p) catch { case _: Throwable => () } }
      } finally walk.close()
    }
  }

  @Benchmark
  def compile(): Unit = {
    val args = (baseArgs ++ Array("-d", outDir.toAbsolutePath.toString) ++ sourceFiles)
    val reporter = storeReporterCtor.newInstance(null).asInstanceOf[AnyRef]
    // Driver.process returns a Reporter; we reflectively check `hasErrors`.
    val result = processMethod.invoke(driver, args, reporter)
    val hasErrorsM = result.getClass.getMethod("hasErrors")
    val hasErrors = hasErrorsM.invoke(result).asInstanceOf[java.lang.Boolean]
    if (hasErrors.booleanValue()) {
      val errorCountM = result.getClass.getMethod("errorCount")
      val ec = errorCountM.invoke(result).asInstanceOf[java.lang.Integer]
      throw new RuntimeException(s"[bench-ab] compilation failed: $ec errors (compiler=$compiler)")
    }
  }
}
