package dotty.tools.dotc.util

import scala.annotation.tailrec

import dotty.tools.*

/**
 * A chained hash set holding its elements with strong references, used for hash-consing
 * tables whose lifetime is bounded externally: they are cleared wholesale via `clear()`
 * at every run boundary (ContextBase.reset()), so entries never need to be collected
 * individually. Structurally this mirrors WeakHashSet (same bucket-chain layout and
 * probe order) but drops the weak-reference machinery: no ReferenceQueue to poll on
 * every operation, no stale-entry unlinking, and no per-candidate `Reference.get`
 * dereference during probes.
 *
 * This Set implementation cannot hold null. Any attempt to put a null in it will result
 * in a NullPointerException.
 *
 * This set implementation is not thread safe.
 */
abstract class StrongHashSet[A <: AnyRef](initialCapacity: Int = 8, loadFactor: Double = 0.5) extends MutableSet[A] {

  import StrongHashSet.*

  /**
   * the number of elements in this set
   */
  protected var count = 0

  /**
   * from a specified initial capacity compute the capacity we'll use as being the next
   * power of two equal to or greater than the specified initial capacity
   */
  private def computeCapacity = {
    if (initialCapacity < 0) throw new IllegalArgumentException("initial capacity cannot be less than 0")
    var candidate = 1
    while (candidate < initialCapacity)
      candidate *= 2
    candidate
  }
  private val initialTableCapacity = computeCapacity

  /**
   * the underlying table of entries which is an array of Entry linked lists
   */
  protected var table = new Array[Entry[A] | Null](initialTableCapacity)

  /**
   * cached `table.length - 1`, used by `index` to map a hashcode to a bucket
   * (the table length is always a power of two, so this is the bucket mask).
   */
  private var mask = table.length - 1

  /**
   * the limit at which we'll increase the size of the hash table
   */
  protected var threshold = computeThreshold

  private def computeThreshold: Int = (table.size * loadFactor).ceil.toInt

  protected def hash(key: A): Int
  protected def isEqual(x: A, y: A): Boolean = x.equals(y)

  /** Turn hashcode `x` into a table index */
  protected def index(x: Int): Int = x & mask

  /**
   * remove a single entry from a linked list in a given bucket
   */
  private def remove(bucket: Int, prevEntry: Entry[A] | Null, entry: Entry[A]): Unit = {
    Stats.record(statsItem("remove"))
    prevEntry match {
      case null => table(bucket) = entry.tail
      case _ => prevEntry.tail = entry.tail
    }
    count -= 1
  }

  /**
   * Double the size of the internal table when the load factor is exceeded.
   */
  protected def resize(): Unit = {
    Stats.record(statsItem("resize"))
    val oldTable = table
    table = new Array[Entry[A] | Null](oldTable.size * 2)
    mask = table.length - 1
    threshold = computeThreshold

    @tailrec
    def tableLoop(oldBucket: Int): Unit = if (oldBucket < oldTable.size) {
      @tailrec
      def linkedListLoop(entry: Entry[A] | Null): Unit = entry match {
        case null => ()
        case _ =>
          val bucket = index(entry.hash)
          val oldNext = entry.tail
          entry.tail = table(bucket)
          table(bucket) = entry
          linkedListLoop(oldNext)
      }
      linkedListLoop(oldTable(oldBucket))

      tableLoop(oldBucket + 1)
    }
    tableLoop(0)
  }

  def lookup(elem: A): A | Null = (elem: A | Null) match {
    case null => throw new NullPointerException("StrongHashSet cannot hold nulls")
    case _ =>
      Stats.record(statsItem("lookup"))
      val h = hash(elem)
      val bucket = index(h)

      @tailrec
      def linkedListLoop(entry: Entry[A] | Null): A | Null = entry match {
        case null                    => null
        case _                       =>
          if entry.hash == h && isEqual(elem, entry.elem) then entry.elem
          else linkedListLoop(entry.tail)
      }

      linkedListLoop(table(bucket))
  }

  protected def addEntryAt(bucket: Int, elem: A, elemHash: Int, oldHead: Entry[A] | Null): A = {
    Stats.record(statsItem("addEntryAt"))
    table(bucket) = new Entry(elem, elemHash, oldHead)
    count += 1
    if (count > threshold) resize()
    elem
  }

  def put(elem: A): A = (elem: A | Null) match {
    case null => throw new NullPointerException("StrongHashSet cannot hold nulls")
    case _    =>
      Stats.record(statsItem("put"))
      val h = hash(elem)
      val bucket = index(h)
      val oldHead = table(bucket)

      @tailrec
      def linkedListLoop(entry: Entry[A] | Null): A = entry match {
        case null                    => addEntryAt(bucket, elem, h, oldHead)
        case _                       =>
          if entry.hash == h && isEqual(elem, entry.elem) then entry.elem
          else linkedListLoop(entry.tail)
      }

      linkedListLoop(oldHead)
  }

  def +=(elem: A): Unit = put(elem)

  def -=(elem: A): Unit = (elem: A | Null) match {
    case null =>
    case _ =>
      Stats.record(statsItem("-="))
      val h = hash(elem)
      val bucket = index(h)

      @tailrec
      def linkedListLoop(prevEntry: Entry[A] | Null, entry: Entry[A] | Null): Unit =
        if entry != null then
          if entry.hash == h && isEqual(elem, entry.elem) then remove(bucket, prevEntry, entry)
          else linkedListLoop(entry, entry.tail)

      linkedListLoop(null, table(bucket))
  }

  def clear(resetToInitial: Boolean): Unit =
    if (count != 0 || (resetToInitial && table.length != initialTableCapacity)) {
      if resetToInitial then table = new Array[Entry[A] | Null](initialTableCapacity)
      else
        var bucket = 0
        while bucket < table.length do
          table(bucket) = null
          bucket += 1
      mask = table.length - 1
      threshold = computeThreshold
      count = 0
    }

  def size: Int = count

  // Iterator over all the elements in this set in no particular order
  override def iterator: Iterator[A] =
    new collection.AbstractIterator[A] {

      /**
       * the bucket currently being examined. Initially it's set past the last bucket and will be decremented
       */
      private var currentBucket: Int = table.size

      /**
       * the entry that was last examined
       */
      private var entry: Entry[A] | Null = null

      def hasNext: Boolean = {
        while (entry == null && currentBucket > 0) {
          currentBucket -= 1
          entry = table(currentBucket)
        }
        entry != null
      }

      def next(): A =
        val e = entry
        if e == null then
          throw new IndexOutOfBoundsException("next on an empty iterator")
        else
          entry = e.tail
          e.elem
    }

  protected def statsItem(op: String): String = {
    val prefix = "StrongHashSet."
    val suffix = getClass.getSimpleName
    s"$prefix$op $suffix"
  }
}

/**
 * Companion object for StrongHashSet
 */
object StrongHashSet {
  /**
   * A single entry in a StrongHashSet: the element held strongly, plus a cached hash code
   * and a link to the next Entry in the same bucket
   */
  class Entry[A](val elem: A, val hash: Int, var tail: Entry[A] | Null)
}
