#!/usr/bin/python
#
# 2009 Mike Perry, Karsten Loesing

"""
Speedracer

Speedracer continuously requests the Tor design paper over the Tor network
and measures how long circuit building and downloading takes.
"""

import atexit
import socket
from time import time,strftime
import sys
import urllib2
import os
import traceback
import copy
import threading
import ConfigParser

sys.path.append("../../")

from TorCtl.TorUtil import plog

from TorCtl.TorUtil import control_port, control_host, tor_port, tor_host, control_pass

from TorCtl import PathSupport,SQLSupport,TorCtl,TorUtil

sys.path.append("../libs")
from SocksiPy import socks

user_agent = "Mozilla/4.0 (compatible; MSIE 6.0; Windows NT 5.1; .NET CLR 1.0.3705; .NET CLR 1.1.4322)"

#          cutoff percent                URL
urls =         [(10,          "https://128.174.236.117/4096k"),
                (20,          "https://128.174.236.117/2048k"),
                (30,          "https://128.174.236.117/1024k"),
                (60,          "https://128.174.236.117/512k"),
                (75,          "https://128.174.236.117/256k"),
                (100,         "https://128.174.236.117/128k")]


# Do NOT modify this object directly after it is handed to PathBuilder
# Use PathBuilder.schedule_selmgr instead.
# (Modifying the arguments here is OK)
__selmgr = PathSupport.SelectionManager(
      pathlen=2,
      order_exits=False,
      percent_fast=100,
      percent_skip=0,
      min_bw=1024,
      use_all_exits=False, # XXX: need to fix conserve_exits to ensure 443
      uniform=True,
      use_exit=None,
      use_guards=False)

def read_config(filename):
  config = ConfigParser.SafeConfigParser()
  config.read(filename)

  start_pct = config.getint('BwAuthority', 'start_pct')
  stop_pct = config.getint('BwAuthority', 'stop_pct')

  nodes_per_slice = config.getint('BwAuthority', 'nodes_per_slice')
  save_every = config.getint('BwAuthority', 'save_every')
  circs_per_node = config.getint('BwAuthority', 'circs_per_node')
  out_dir = config.get('BwAuthority', 'out_dir')

  return (start_pct,stop_pct,nodes_per_slice,save_every,circs_per_node,out_dir)

def choose_url(percentile):
  for (pct, url) in urls:
    if percentile < pct:
      #return url
      return "https://86.59.21.36/torbrowser/dist/tor-im-browser-1.2.0_ru_split/tor-im-browser-1.2.0_ru_split.part01.exe"
  raise PathSupport.NoNodesRemain("No nodes left for url choice!")

# Note: be careful writing functions for this class. Remember that
# the PathBuilder has its own thread that it recieves events on
# independent from your thread that calls into here.
class BwScanHandler(PathSupport.PathBuilder):
  def get_exit_node(self):
    return copy.copy(self.last_exit) # GIL FTW

  def attach_sql_listener(self, db_uri):
    plog("DEBUG", "Got sqlite: "+db_uri)
    SQLSupport.setup_db(db_uri, echo=False, drop=True)
    self.add_event_listener(SQLSupport.ConsensusTrackerListener())
    self.add_event_listener(SQLSupport.StreamListener())

  def write_sql_stats(self, percent_skip, percent_fast, rfilename=None):
    if not rfilename:
      rfilename="./data/stats/sql-"+time.strftime("20%y-%m-%d-%H:%M:%S")
    cond = threading.Condition()
    def notlambda(h):
      cond.acquire()
      SQLSupport.RouterStats.write_stats(file(rfilename, "w"),
                            percent_skip, percent_fast,
                            order_by=SQLSupport.RouterStats.sbw,
                            recompute=True)
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()

  def write_strm_bws(self, percent_skip, percent_fast, rfilename=None):
    if not rfilename:
      rfilename="./data/stats/bws-"+time.strftime("20%y-%m-%d-%H:%M:%S")
    cond = threading.Condition()
    def notlambda(h):
      cond.acquire()
      SQLSupport.RouterStats.write_bws(file(rfilename, "w"),
                            percent_skip, percent_fast,
                            order_by=SQLSupport.RouterStats.sbw,
                            recompute=False) # XXX: Careful here..
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()

  def set_pct_rstr(self, percent_skip, percent_fast):
    def notlambda(sm):
      sm.percent_fast=percent_fast
      sm.percent_skip=percent_skip
    self.schedule_selmgr(notlambda)

  def reset_stats(self):
    def notlambda(this): 
      this.reset()
    self.schedule_low_prio(notlambda)

  def save_sql_file(self, sql_file, new_file):
    cond = threading.Condition()
    def notlambda(this):
      cond.acquire()
      SQLSupport.tc_session.close()
      try:
        os.rename(sql_file, new_file)
      except Exception,e:
        plog("WARN", "Error moving sql file: "+str(e))
      SQLSupport.setup_db('sqlite:////'+sql_file, echo=False, drop=True)
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()

  def commit(self):
    # FIXME: This needs two stages+condition to really be correct
    def notlambda(this): 
      this.run_all_jobs = True
    self.schedule_immediate(notlambda)

  def close_circuits(self):
    cond = threading.Condition()
    def notlambda(this):
      cond.acquire()
      this.close_all_circuits()
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()

  def new_exit(self):
    cond = threading.Condition()
    def notlambda(this):
      cond.acquire()
      this.new_nym = True # GIL hack
      lines = this.c.sendAndRecv("SIGNAL CLEARDNSCACHE\r\n")
      for _,msg,more in lines:
        plog("DEBUG", msg)
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()

  def is_count_met(self, count, position=0):
    cond = threading.Condition()
    cond._finished = True # lol python haxx. Could make subclass, but why?? :)
    def notlambda(this):
      cond.acquire()
      for r in this.sorted_r:
        if len(r._generated) > position:
          if r._generated[position] < count:
            cond._finished = False
            break
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()
    return cond._finished

  def rank_to_percent(self, rank):
    cond = threading.Condition()
    def notlambda(this):
      cond.acquire()
      cond._pct = (100.0*rank)/len(this.sorted_r) # lol moar haxx
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()
    return cond._pct

  def percent_to_rank(self, pct):
    cond = threading.Condition()
    def notlambda(this):
      cond.acquire()
      cond._rank = int(round((pct*len(this.sorted_r))/100.0,0)) # lol moar haxx
      cond.notify()
      cond.release()
    cond.acquire()
    self.schedule_low_prio(notlambda)
    cond.wait()
    cond.release()
    return cond._rank

def http_request(address):
  ''' perform an http GET-request and return 1 for success or 0 for failure '''

  request = urllib2.Request(address)
  request.add_header('User-Agent', user_agent)

  try:
    reply = urllib2.urlopen(request)
    decl_length = reply.info().get("Content-Length")
    read_len = len(reply.read())
    plog("DEBUG", "Read: "+str(read_len)+" of declared "+str(decl_length))
    return 1
  except (ValueError, urllib2.URLError):
    plog('ERROR', 'The http-request address ' + address + ' is malformed')
    return 0
  except (IndexError, TypeError):
    plog('ERROR', 'An error occured while negotiating socks5 with Tor')
    return 0
  except KeyboardInterrupt:
    raise KeyboardInterrupt
  except:
    plog('ERROR', 'An unknown HTTP error occured')
    traceback.print_exc()
    return 0 

def speedrace(hdlr, start_pct, stop_pct, circs_per_node, save_every, out_dir):
  hdlr.set_pct_rstr(start_pct, stop_pct)

  attempt = 0
  successful = 0
  while not hdlr.is_count_met(circs_per_node):
    hdlr.new_exit()
    
    attempt += 1
    
    t0 = time()
    ret = http_request(choose_url(start_pct))
    delta_build = time() - t0
    if delta_build >= 550.0:
      plog('NOTICE', 'Timer exceeded limit: ' + str(delta_build) + '\n')

    build_exit = hdlr.get_exit_node()
    if ret == 1:
      successful += 1
      plog('DEBUG', str(start_pct) + '-' + str(stop_pct) + '% circuit build+fetch took ' + str(delta_build) + ' for ' + str(build_exit))
    else:
      plog('DEBUG', str(start_pct)+'-'+str(stop_pct)+'% circuit build+fetch failed for ' + str(build_exit))

    if save_every and ret and successful and (successful % save_every) == 0:
      race_time = strftime("20%y-%m-%d-%H:%M:%S")
      hdlr.commit()
      hdlr.close_circuits()
      lo = str(round(start_pct,1))
      hi = str(round(stop_pct,1))
      hdlr.write_sql_stats(start_pct, stop_pct, os.getcwd()+'/'+out_dir+'/sql-'+lo+':'+hi+"-"+str(successful)+"-"+race_time)
      hdlr.write_strm_bws(start_pct, stop_pct, os.getcwd()+'/'+out_dir+'/bws-'+lo+':'+hi+"-"+str(successful)+"-"+race_time)

  plog('INFO', str(start_pct) + '-' + str(stop_pct) + '% ' + str(successful) + ' fetches took ' + str(attempt) + ' tries.')

def main(argv):
  TorUtil.read_config(argv[1]) 
  (start_pct,stop_pct,nodes_per_slice,save_every,
         circs_per_node,out_dir) = read_config(argv[1])
 
  try:
    (c,hdlr) = setup_handler()
  except Exception, e:
    plog("WARN", "Can't connect to Tor: "+str(e))

  sql_file = os.getcwd()+'/'+out_dir+'/bwauthority.sqlite'
  hdlr.attach_sql_listener('sqlite:///'+sql_file)

  # set SOCKS proxy
  socks.setdefaultproxy(socks.PROXY_TYPE_SOCKS5, tor_host, tor_port)
  socket.socket = socks.socksocket

  while True:
    pct = start_pct
    plog('INFO', 'Beginning time loop')
    
    while pct < stop_pct:
      pct_step = hdlr.rank_to_percent(nodes_per_slice)
      hdlr.reset_stats()
      hdlr.commit()
      plog('DEBUG', 'Reset stats')

      speedrace(hdlr, pct, pct+pct_step, circs_per_node, save_every, out_dir)

      plog('DEBUG', 'speedroced')
      hdlr.commit()
      hdlr.close_circuits()

      lo = str(round(pct,1))
      hi = str(round(pct+pct_step,1))
      
      hdlr.write_sql_stats(pct, pct+pct_step, os.getcwd()+'/'+out_dir+'/sql-'+lo+':'+hi+"-done-"+strftime("20%y-%m-%d-%H:%M:%S"))
      hdlr.write_strm_bws(pct, pct+pct_step, os.getcwd()+'/'+out_dir+'/bws-'+lo+':'+hi+"-done-"+strftime("20%y-%m-%d-%H:%M:%S"))
      plog('DEBUG', 'Wrote stats')
      pct += pct_step
      hdlr.save_sql_file(sql_file, "db-"+str(lo)+":"+str(hi)+"-"+strftime("20%y-%m-%d-%H:%M:%S")+".sqlite")

def cleanup(c, f):
  plog("INFO", "Resetting __LeaveStreamsUnattached=0 and FetchUselessDescriptors="+f)
  try:
    c.set_option("__LeaveStreamsUnattached", "0")
    c.set_option("FetchUselessDescriptors", f)
  except TorCtl.TorCtlClosed:
    pass

def setup_handler():
  plog('INFO', 'Connecting to Tor...')
  s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
  s.connect((control_host,control_port))
  c = PathSupport.Connection(s)
  #c.debug(file("control.log", "w", buffering=0))
  c.authenticate(control_pass)
  h = BwScanHandler(c, __selmgr)

  c.set_event_handler(h)

  c.set_events([TorCtl.EVENT_TYPE.STREAM,
          TorCtl.EVENT_TYPE.BW,
          TorCtl.EVENT_TYPE.NEWCONSENSUS,
          TorCtl.EVENT_TYPE.NEWDESC,
          TorCtl.EVENT_TYPE.CIRC,
          TorCtl.EVENT_TYPE.STREAM_BW], True)

  c.set_option("__LeaveStreamsUnattached", "1")
  f = c.get_option("FetchUselessDescriptors")[0][1]
  c.set_option("FetchUselessDescriptors", "1")
  atexit.register(cleanup, *(c, f))
  return (c,h)

def usage(argv):
  print "Usage: "+argv[0]+" <configfile>"
  return

# initiate the program
if __name__ == '__main__':
  try:
    if len(sys.argv) < 2: usage(sys.argv)
    else: main(sys.argv)
  except KeyboardInterrupt:
    plog('INFO', "Ctrl + C was pressed. Exiting ... ")
    traceback.print_exc()
  except Exception, e:
    plog('ERROR', "An unexpected error occured.")
    traceback.print_exc()
