package dotty.tools.benchmarks.milljavalib

import java.io.{File, PrintStream}
import java.nio.file.{Files, Path, Paths}
import java.util.concurrent.TimeUnit
import scala.compiletime.uninitialized

import org.openjdk.jmh.annotations._

import dotty.tools.dotc.{Driver, Compiler, report}
import dotty.tools.dotc.core.Contexts.{Context, ContextBase}
import dotty.tools.dotc.reporting.{Reporter, StoreReporter}
import dotty.tools.io.AbstractFile
import dotty.tools.FatalError

/**
 * JMH benchmark that does a clean Scala 3 compilation of every Mill `libs.javalib`
 * (and transitive upstream module) source file using the LOCAL non-bootstrapped
 * `scala3-compiler` classes that are placed on the classpath by run-bench.sh.
 *
 * Inputs are read once at @Setup time from text files under
 * `bench-mill-javalib/inputs/`:
 *
 *   - source-files.txt       paths of every .scala/.java to compile
 *                            (relative to bench-mill-javalib/sources/)
 *   - compile-classpath.txt  third-party jars libs.javalib needs at compile time
 *   - scalac-options.txt     ALL scalac flags, including `-Xplugin:` entries
 *
 * The compile output is dropped into a temp dir that is wiped between
 * iterations so we always measure a "clean compile".
 */
object MillJavalibBenchmark {
  // The bench root is the directory containing the `bench-mill-javalib`
  // tree at runtime. We resolve it from the `bench.dir` system property
  // set by run-bench.sh, falling back to a default that matches the layout
  // of this repository.
  def benchDir: Path = {
    val sysprop = System.getProperty("bench.dir")
    if (sysprop != null && sysprop.nonEmpty) Paths.get(sysprop)
    else Paths.get("/Users/lihaoyi/Github/scala3/bench-mill-javalib")
  }
}

/** A driver that suppresses banner output and surfaces errors via exception. */
class SilentDriver extends Driver {
  override def doCompile(compiler: Compiler, files: List[AbstractFile])(using ctx: Context): Reporter =
    if (files.nonEmpty)
      try {
        val run = compiler.newRun
        run.compile(files)
        ctx.reporter
      } catch {
        case ex: FatalError =>
          report.error(ex.getMessage)
          ctx.reporter
      }
    else ctx.reporter
}

@State(Scope.Benchmark)
@BenchmarkMode(Array(Mode.AverageTime))
@OutputTimeUnit(TimeUnit.MILLISECONDS)
class MillJavalibBenchmark {

  // Filled in @Setup
  private var driver: SilentDriver = uninitialized
  private var baseArgs: Array[String] = uninitialized      // -classpath, -Xplugin, scalac options (NO -d, NO sources)
  private var sourceFiles: Array[String] = uninitialized   // absolute paths under sources/

  // Per-iteration scratch
  private var outDir: Path = uninitialized

  @Setup(Level.Trial)
  def setupTrial(): Unit = {
    val root = MillJavalibBenchmark.benchDir
    val inputs = root.resolve("inputs")
    val sources = root.resolve("sources")

    def readLines(p: Path): List[String] = {
      val it = Files.lines(p)
      try {
        val b = List.newBuilder[String]
        it.forEach(s => if (s.trim.nonEmpty) b += s.trim)
        b.result()
      } finally it.close()
    }

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

    driver = new SilentDriver

    val pluginCount = opts.count(_.startsWith("-Xplugin:"))
    System.out.println(s"[bench] benchDir       = $root")
    System.out.println(s"[bench] sources        = ${sourceFiles.length} files")
    System.out.println(s"[bench] compile cp     = ${cp.size} jars")
    System.out.println(s"[bench] scalac opts    = ${opts.size} (with $pluginCount -Xplugin entries)")
  }

  @Setup(Level.Iteration)
  def setupIteration(): Unit = {
    outDir = Files.createTempDirectory("mill-javalib-bench-out-")
  }

  @TearDown(Level.Iteration)
  def teardownIteration(): Unit = {
    if (outDir != null) {
      // best-effort recursive delete
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
    // Pass a StoreReporter so warnings and infos are captured silently rather
    // than printed to stdout/stderr between iterations (which both adds noise
    // to the JMH log and adds I/O time to the measurement).
    val reporter: Reporter = new StoreReporter(null)
    val result = driver.process(args, reporter)
    if (result.hasErrors) {
      System.err.println(s"[bench] compilation FAILED with ${result.errorCount} error(s)")
      // Surface the actual error messages so the failure is debuggable.
      result.allErrors.foreach(e => System.err.println(s"  $e"))
      throw new RuntimeException(s"compilation failed: ${result.errorCount} errors")
    }
  }
}
