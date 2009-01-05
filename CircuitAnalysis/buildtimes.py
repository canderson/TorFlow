#!/usr/bin/env python
# uses metatroller to collect circuit build times for 5% slices of guard nodes
# [OUTPUT] one directory, with three files: StatsHandler aggregate stats file, file with all circuit events (for detailed reference), file with just buildtimes

import socket,sys,time,getopt,os
from TorCtl.TorUtil import meta_port,meta_host,control_port,control_host
from TorCtl.StatsSupport import StatsHandler
from TorCtl import PathSupport, TorCtl
__selmgr = PathSupport.SelectionManager(
      pathlen=3,
      order_exits=True,
      percent_fast=80,
      percent_skip=0,
      min_bw=1024,
      use_all_exits=True,
      uniform=True,
      use_exit=None,
      use_guards=True,
      restrict_guards=True)

class Connection(PathSupport.Connection):
  """ thread quits when required number of circuits found, otherwise identical"""
  def __init__(self,s):
    PathSupport.Connection.__init__(self,s)
  def _loop(self):
    while 1:
      try:
        isEvent, reply = self._read_reply()
      except:
        self._err(sys.exc_info())
        return

      if isEvent:
        if self._handler is not None:
          self._eventQueue.put((time.time(), reply))
      else:
        cb = self._queue.get() # atomic..
        cb(reply)

      if self._handler is not None:
        if self._handler.circ_failed + self._handler.circ_built >= self._handler.nstats:
          print 'Finished gathering',self._handler.circ_failed + self._handler.circ_built,'circuits'
          print self._handler.circ_failed,'failed',self._handler.circ_built,'built'
          return 

class StatsGatherer(StatsHandler):
  def __init__(self,c, selmgr,basefile_name,nstats):
    StatsHandler.__init__(self,c, selmgr)

    self.detailfile = open(basefile_name + '.detail','w')
    self.buildtimesfile = open(basefile_name + '.buildtimes','w')
    self.circ_built = 0
    self.nstats = nstats

    # sometimes relevant CircEvents occur before the circ_id is 
    # added to self.circuits, which means they get discarded
    # we track them in self.othercircs: a dictionary of list of events
    self.othercircs = {} 

  def circ_event_str(self,now,circ_event):
    """ returns an string summarizing the circuit event"""
    output = [circ_event.event_name, str(circ_event.circ_id),
        circ_event.status]
    if circ_event.path:
      output.append(",".join(circ_event.path))
    if circ_event.reason:
      output.append("REASON=" + circ_event.reason)
    if circ_event.remote_reason:
      output.append("REMOTE_REASON=" + circ_event.remote_reason)
    output = [now]+ output
    outstr = ' '.join(output) + '\n'
    return outstr
 
  def add_missed_events(self,circ_id):
    """ if there are events for a circuit that were missed, add them"""
    if circ_id in self.othercircs:
      for e_str in self.othercircs[circ_id]:
        self.detailfile.write(e_str)
      self.detailfile.flush()
      # now in self.circuits, so can delete it from self.othercircs
      del self.othercircs[circ_id]
      

  def circ_status_event(self, circ_event):
    """ handles circuit status event """
    now = time.time()
    now = '%3.10f' % now

    if circ_event.circ_id in self.circuits.keys():
      self.add_missed_events(circ_event.circ_id)
      if circ_event.status == 'EXTENDED':
        extend_time = circ_event.arrived_at-self.circuits[circ_event.circ_id].last_extended_at
        self.circuits[circ_event.circ_id].extend_times.append(extend_time)
        self.circuits[circ_event.circ_id].last_extended_at = circ_event.arrived_at

      if circ_event.status == 'BUILT':
        circ = self.circuits[circ_event.circ_id]
        buildtime = reduce(lambda x,y:x+y,circ.extend_times,0.0)
        self.buildtimesfile.write(str(circ.circ_id) + '\t' + str(buildtime) + '\n')
        self.buildtimesfile.flush()

      outstr = self.circ_event_str(now,circ_event)
      self.detailfile.write(outstr)
      self.detailfile.flush()

      # check to see if done gathering data
      if circ_event.status == 'BUILT': self.circ_built += 1
    else:
      #eventstr = 
      #if circ_event.circ_id in self.othercircs.keys():
      if circ_event.circ_id not in self.othercircs.keys():
        self.othercircs[circ_event.circ_id] = []
      self.othercircs[circ_event.circ_id] += [self.circ_event_str(now,circ_event)]
    StatsHandler.circ_status_event(self,circ_event)

def getdata(filename,ncircuits):
  """ starts stat gathering thread """

  s = socket.socket(socket.AF_INET,socket.SOCK_STREAM)
  s.connect((control_host,control_port))
  c = Connection(s)
  c.authenticate()  # also launches thread...
  h = StatsGatherer(c,__selmgr,filename,ncircuits)
  c.set_event_handler(h)

  c.set_events([TorCtl.EVENT_TYPE.STREAM,
                TorCtl.EVENT_TYPE.BW,
                TorCtl.EVENT_TYPE.NS,
                TorCtl.EVENT_TYPE.CIRC,
                TorCtl.EVENT_TYPE.STREAM_BW,
                TorCtl.EVENT_TYPE.NEWDESC], True)
  return c

def setargs():
  ncircuits = ""
  dirname = ""
  filename = ""
  if len(sys.argv[1:]) < 3:
    usage()
    sys.exit(2)
  try:
    opts,args = getopt.getopt(sys.argv[1:],"p:n:d:")
  except getopt.GetoptError,err:
    print str(err)
    usage()
  ncircuits=None
  percentile=None
  dirname=""
  for o,a in opts:
    if o == '-n': 
      if a.isdigit(): ncircuits = int(a)
      else: usage()
    elif o == '-d': dirname = a  #directory where output files go 
    elif o == '-p':
      if a.isdigit(): percentile = int(a)
      else: usage()
    else:
      assert False, "Bad option"
  return ncircuits,percentile,dirname

def usage():
    print 'usage: statscontroller.py [-p <#percentile>]  -n <# circuits> -d <output dir name>'
    sys.exit(1)


def guardslice(p,ncircuits,dirname):

  print 'Making new directory:',dirname
  if not os.path.isdir(dirname):
    os.mkdir(dirname)
  else:
    print 'Directory',dirname,'exists, not making a new one.'

  print 'Guard percentiles:',p,'to',p+5
  print '#Circuits',ncircuits

  basefile_name = dirname + '/' + str(p) + '-' + str(p+5) + '.' + str(ncircuits)
  aggfile_name =  basefile_name + '.agg'

  __selmgr.percent_fast = p+5
  __selmgr.percent_skip = p
  
  c = getdata(basefile_name,ncircuits)

  for i in xrange(0,ncircuits):
    print 'Building circuit',i
    try:
      # XXX: hrmm.. race conditions on the path_selectior members 
      # for the event handler thread?
      # Probably only if streams end up coming in during this test..
      circ = c.build_circuit(__selmgr.pathlen,__selmgr.path_selector)
      c._handler.circuits[circ.circ_id] = circ
    except TorCtl.ErrorReply,e:
      plog("NOTICE","Error building circuit: " + str(e.args))

  while True:
    time.sleep(1)
    if c._handler.circ_built + c._handler.circ_failed >= ncircuits:
      print 'Done gathering stats for slice',p,'to',p+5,'on',ncircuits
      print c._handler.circ_built,'built',c._handler.circ_failed,'failed' 
      break
  c._handler.write_stats(aggfile_name)

def main():
  ncircuits,p,dirname = setargs()

  if p is None:
    # do all
    for p in xrange(0,100,5):
      guardslice(p,ncircuits,dirname)
  else:
    guardslice(p,ncircuits,dirname)
if __name__ == '__main__':
  main()