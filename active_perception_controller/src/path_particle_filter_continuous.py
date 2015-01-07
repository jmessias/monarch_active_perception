#!/usr/bin/env python

import sys
import os.path
import time
import math
import random
import cPickle as pickle

import numpy as np
import scipy.linalg
import scipy.signal

import roslib
SCOUT_NAVIGATION_DIR = roslib.packages.get_pkg_dir('scout_navigation')
MAPS_DIR = roslib.packages.get_pkg_dir('maps')

import rospy
import rosbag
import tf
from sensor_msgs.msg import *
from geometry_msgs.msg import *

sys.path.append( SCOUT_NAVIGATION_DIR )
import planner



DEFAULT_MAP = os.path.join(MAPS_DIR, "EPFL-v02cr_nav.yaml")
MAP_FRAME = 'map'
DEFAULT_CLEARANCE = 0.5
N_PARTICLES = 500
FACTOR = 3

# sensor model
ANGLE_DELTA = 0.1 # 0.003 # hacked for Hokuyo (approx angle_increment/2)
# observation model
OCC_LOGPROB  =  5.0
FREE_LOGPROB = -1.0



def fix_angle(a):
    pi = math.pi
    return (a+pi)%(2*pi) - pi


class PathParticleFilter:
    # random sample injection
    RANDOM_FRAC = 0 # 0.01

    points = None  # [index,coordinate]
    logprob = None  # [index]
    
    def __init__(self, path, factor):
        """path is a list of pairs (x,y); factor w.r.t. path length"""
        self.path = path
        n = len(path)
        p = np.array(path)
        d = np.sqrt(np.sum((p[1:]-p[:-1])**2, axis=1))
        A = np.vstack([np.zeros(n-1),
                       np.tri(n-1)])
        cum = np.dot(A, d)
        plen  = cum[-1]
        total = int(math.ceil(factor*plen))
        # samples in total path len units
        l = plen*np.random.uniform(size=total)
        # samples segment indices
        i = (cum[:,None] <= l[None,:]).sum(axis=0) - 1
        # and offsets
        o = l - cum[i]
        self.points = p[i] + (p[i+1]-p[i])*(o/d[i])[:,None]
        self.logprob = np.zeros(total)
        #print "path: length=%s, took %s samples"%(n,total)

    def apply_importance(self, func):
        """func maps an array of [index,coordinate] points to importances of [index]"""
        self.logprob += func(self.points)

    def resample(self):
        # get normalized probabilities
        n = int(math.ceil( self.RANDOM_FRAC*len(self.logprob) ))
        m = len(self.logprob) - n
        w = np.exp(self.logprob)
        w /= w.sum()
        # re-sample m samples
        si  = scipy.linalg.toeplitz(w, np.zeros_like(w)).sum(axis=1)
        rj  = (np.random.random() + np.arange(m)) / m
        dij = si[:,None] - rj[None,:]
        dij[dij<0] = np.inf
        k   = dij.argmin(axis=0)
        resampled = self.points[k]
        if n>0:
            # generate n new samples
            total = self.cells.shape[1]
            samples = [ tuple(self.cells[:,random.randint(0,total-1)]) for i in xrange(n) ]
            new = np.array(samples, dtype=int)
            self.points = np.vstack((resampled, new))
        else:
            self.points = resampled
        self.logprob = np.zeros(len(self.points))

    # TODO:
    def diffusion(self):
        raise Exception, "Not yet implemented"

class MapParticleModel:
    filters = None
    paths = cells = None
    
    def __init__(self, mapfile, clearance=DEFAULT_CLEARANCE):
        self.pln = planner.FastMarching()
        self.pln.load_map(mapfile)
        self.pln.setup()
        maxd = clearance/self.pln.scale
        self.feasible = self.pln.distmap > maxd
        candidates = self.feasible.nonzero()
        imin = candidates[0].min()
        imax = candidates[0].max()
        jmin = candidates[1].min()
        jmax = candidates[1].max()
        (xmin, ymax) = self.pln.i2p((imin, jmin))
        (xmax, ymin) = self.pln.i2p((imax, jmax))
        self.bbox = (xmin, xmax, ymin, ymax)

    def sample_points(self, total):
        """generate positions in map free space"""
        points  = []
        (xmin, xmax, ymin, ymax) = self.bbox
        while len(points)<total:
            x = random.uniform(xmin, xmax)
            y = random.uniform(ymin, ymax)
            (i, j) = self.pln.p2i((x, y))
            if self.feasible[i,j]:
                points.append((x, y))
        return points

    # def gen_paths_full(self, total):
    #     """generate paths in a fully connected graph"""
    #     points = self.sample_points(total)
    #     self.paths = []
    #     for (i,g) in enumerate(points[:-1]):
    #         print "-- Solving for particle %d of %d"%(i+1,len(points))
    #         t1 = time.time()
    #         self.pln.solve(g)
    #         for o in points[i+1:]:
    #             p = np.array( self.pln.get_path(o[0], o[1], step=self.pln.scale) )
    #             self.paths.append(p)
    #         t2 = time.time()
    #         print "   took %f ms"%(1000*(t2-t1))

    def gen_paths_pair(self, total):
        """generate paths between pairs of samples"""
        points = self.sample_points(total)
        self.paths = []
        for k in xrange(0, len(points)-1, 2):
            (o, g) = points[k:k+2]
            if self.pln.solve(g) and self.pln.validate(o):
                p = np.array( self.pln.get_path(o[0], o[1], step=self.pln.scale) )
                self.paths.append(p)
            else:
                print "*** discarding pair", o, g

    def sample_paths(self, factor):
        """create factor*L particle for each path and create a cell->path dict"""
        assert self.paths is not None, "no paths were generated"
        self.filters = [ PathParticleFilter(p, factor) for p in self.paths ]


    # def find_swept_cells(self, scan, pose):
    #     """scan is a ROS scan msg; pose is a (position, quaternion) tuple"""
    #     s = self.pln.scale
    #     #
    #     (position, quaternion) = pose
    #     z = np.array(scan.ranges)
    #     a = np.arange(scan.angle_min, scan.angle_min+scan.angle_increment*len(z), scan.angle_increment)
    #     assert len(z)==len(a), "Range and angle arrays do not match"
    #     # discard invalid ranges
    #     valid = np.isfinite(z)
    #     zv = z[valid]
    #     valid[valid] = (zv>scan.range_min) & (zv<scan.range_max)
    #     assert valid.any(), "No valid scan line found"
    #     z, a = z[valid], a[valid]
    #     # obtain laser pointcloud in device coordinates
    #     xl = z*np.cos(a)
    #     yl = z*np.sin(a)
    #     # transform pointcloud according to tf
    #     T = tf.transformations.quaternion_matrix(quaternion)
    #     R = T[0:3,0:3]
    #     pl = np.vstack([xl, yl, np.zeros_like(z)])
    #     pb = np.dot(R, pl)
    #     xli = pb[0,:]
    #     yli = pb[1,:]
    #     a = np.arctan2(yli, xli)
    #     #
    #     # BROKEN CODE: (does not account for arbitrary LIDAR placements)
    #     # z = np.array(scan.ranges)
    #     # a = tr + np.arange(scan.angle_min, scan.angle_min+scan.angle_increment*len(z), scan.angle_increment)
    #     # a = fix_angle(a)
    #     # assert len(z)==len(a), "Range and angle arrays do not match"
    #     # valid = np.isfinite(z)
    #     # zv = z[valid]
    #     # valid[valid] = (zv>scan.range_min) & (zv<scan.range_max)
    #     # assert valid.any(), "No valid scan line found"
    #     # z, a = z[valid], a[valid]
    #     #
    #     t1 = time.time()
    #     # determine quadrants
    #     q = 3*np.ones_like(a, dtype=int)
    #     q[ (a>=-math.pi/4)   & (a<math.pi/4)   ] = 1
    #     q[ (a>=math.pi/4)    & (a<3*math.pi/4) ] = 2
    #     q[ (a>=-3*math.pi/4) & (a<-math.pi/4)  ] = 4
    #     #print "q:", q
    #     # determine sweep
    #     kmax = int(math.ceil(max( np.abs(z*np.cos(a)/s).max(), np.abs(z*np.sin(a)/s).max() )))
    #     k = np.arange(kmax+1)
    #     #print "k:", k[0], "-", k[-1]
    #     # sweep indices of hits
    #     kh = np.empty_like(a, dtype=int)
    #     kh[q==1] =  np.around(z[q==1]*np.cos(a[q==1])/s)
    #     kh[q==2] =  np.around(z[q==2]*np.sin(a[q==2])/s)
    #     kh[q==3] = -np.around(z[q==3]*np.cos(a[q==3])/s)
    #     kh[q==4] = -np.around(z[q==4]*np.sin(a[q==4])/s)
    #     #print "kh:", kh
    #     # obtain [i,k] matrix where 0=unknown, 1=free, 2=hit
    #     # where i is the angle index and k is the sweep index
    #     m = np.zeros((len(a), len(k)), dtype=int)
    #     m[ np.arange(len(k))[None,:] < kh[:,None] ] = 1
    #     m[ xrange(len(a)), kh ] = 2
    #     print "[sweeps: %f ms]"%(1000*(time.time()-t1))
    #     #
    #     t1 = time.time()
    #     # determine index offsets of sweep
    #     j = np.empty((len(a), len(k)), dtype=int)
    #     i = np.empty((len(a), len(k)), dtype=int)
    #     # Q I
    #     j[q==1,:] = k[None,:]
    #     i[q==1,:] = -np.around(np.tan(a[q==1,None])*k[None,:])
    #     # Q II
    #     j[q==2,:] = np.around((1/np.tan(a[q==2,None]))*k[None,:])
    #     i[q==2,:] = -k[None,:]
    #     # Q III
    #     j[q==3,:] = -k[None,:]
    #     i[q==3,:] =  np.around(np.tan(a[q==3,None])*k[None,:])
    #     # Q IV
    #     j[q==4,:] = -np.around((1/np.tan(a[q==4,None]))*k[None,:])
    #     i[q==4,:] =  k[None,:]
    #     print "[sweep in index coords: %f ms]"%(1000*(time.time()-t1))
    #     # determine swept map indices found free
    #     t1 = time.time()
    #     (ir, jr) = self.pln.p2i((position[0], position[1]))
    #     imfree = ir + i[m==1]
    #     jmfree = jr + j[m==1]
    #     imocc  = ir + i[m==2]
    #     jmocc  = jr + j[m==2]
    #     # check map limits
    #     valid = (imfree>=0) & (imfree<self.pln.H) & (jmfree>=0) & (jmfree<self.pln.W)
    #     imfree = imfree[valid]
    #     jmfree = jmfree[valid]
    #     valid = (imocc>=0) & (imocc<self.pln.H) & (jmocc>=0) & (jmocc<self.pln.W)
    #     imocc = imocc[valid]
    #     jmocc = jmocc[valid]
    #     # # prune repeated cells -- TODO: rethink this method; doesn't seem to work at all
    #     # matches = np.logical_and(imfree[1:]==imfree[:-1],
    #     #                          jmfree[1:]==jmfree[:-1])
    #     # unique = np.ones_like(jmfree, dtype=bool)
    #     # unique[:-1] = np.logical_not(matches)
    #     # print "prunning repeated free cells:", len(imfree),
    #     # imfree = imfree[unique]
    #     # jmfree = jmfree[unique]
    #     # print "->", len(imfree)
    #     return (imfree, jmfree, imocc, jmocc)

    def compute_importance(self, scan, pose):
        assert self.filters is not None, "no filters were generated yet"
        ## prepare data
        (position, quaternion) = pose
        z = np.array(scan.ranges)
        a = np.arange(scan.angle_min, scan.angle_min+scan.angle_increment*len(z), scan.angle_increment)
        assert len(z)==len(a), "Range and angle arrays do not match"
        # discard invalid ranges
        valid = np.isfinite(z)
        zv = z[valid]
        valid[valid] = (zv>scan.range_min) & (zv<scan.range_max)
        assert valid.any(), "No valid scan line found"
        zi, ai = z[valid], a[valid]
        # obtain laser pointcloud in device coordinates
        xl = zi*np.cos(ai)
        yl = zi*np.sin(ai)
        # transform pointcloud according to tf
        (xr, yr, zr) = position
        T = tf.transformations.quaternion_matrix(quaternion)
        R = T[0:3,0:3]
        pl = np.vstack([xl, yl, np.zeros_like(zi)])
        pb = np.dot(R, pl)
        xi = pb[0,:] + xr
        yi = pb[1,:] + yr
        ti = np.arctan2(pb[1,:], pb[0,:])
        # compute hit radius
        ri = zi * math.sin(ANGLE_DELTA)
        # compute bounding box
        xmin = (xi-ri).min()
        xmax = (xi+ri).max()
        ymin = (yi-ri).min()
        ymax = (yi+ri).max()
        print "limits:", xmin, xmax, ymin, ymax
        # gather particles
        particles = np.vstack( [ np.hstack( [ pf.points,
                                              i*np.ones(len(pf.points))[:,None],
                                              np.arange(len(pf.points))[:,None] ] )
                                 for (i,pf) in enumerate(self.filters) ] )
        print "particles:", particles.shape
        # select particles within bounding box
        valid = reduce(np.logical_and, [ particles[:,0]>=xmin,
                                         particles[:,0]<=xmax,
                                         particles[:,1]>=ymin,
                                         particles[:,1]<=ymax ] )
        particles = particles[valid]
        pj = particles[:,0]
        qj = particles[:,1]
        print "particles:", particles.shape
        # select particles found occupied
        occ_hits = ( (xi[:,None]-pj[None,:])**2 + (yi[:,None]-qj[None,:])**2 <= ri[:,None]**2 ).any(axis=1)
        print "occ_hits:", occ_hits.shape, occ_hits.sum()
        # select particles found free
        bj  = np.arctan2(qj-yr, pj-xr)
        dj2 = (pj-xr)**2 + (qj-yr)**2
        free_hits = np.logical_and( np.abs(fix_angle(bj[None,:]-ti[:,None])) <= ANGLE_DELTA,
                                    dj2[None,:] < zi[:,None]**2 ).any(axis=1)
        print "free_hits:", free_hits.shape, free_hits.sum()
        
        # BUG: does not find as many free hits as expected even with large ANGLE_DELTA

        plt.scatter([xr], [yr], c='k')
        plt.scatter(xi, yi, c='r', marker='.', linewidth=0)
        plt.scatter(pj, qj, c='b', marker='.', linewidth=0)
        plt.scatter(pj[free_hits], qj[free_hits], c='g', marker='.', linewidth=0)
        plt.show()

        raise Exception







        # determine swept cells
        (imfree, jmfree, imocc, jmocc) = self.find_swept_cells(scan, pose)
        # bounding box of swept cells
        imin = min(imfree.min(), imocc.min())
        imax = max(imfree.max(), imocc.max())
        jmin = min(jmfree.min(), jmocc.min())
        jmax = max(jmfree.max(), jmocc.max())
        ww  = jmax - jmin + 1
        wh  = imax - imin + 1
        # win is a map window containing the log prob of occupancy for each cell
        win = np.zeros((wh, ww))

        # place dirac functions on occupied cells
        win[ imocc-imin, jmocc-jmin ] = self.OCC_LOGPROB
        # convolve with a separable FIR kernel
        win = scipy.signal.sepfir2d(win, self.occkernel, self.occkernel)
        # reduce log prob of free cells
        win[imfree-imin, jmfree-jmin] += self.FREE_LOGPROB

        # hacking I
        # win[ imocc-imin, jmocc-jmin ] = +1

        # hacking II
        # kernel = np.array([[0.5, 1.0, 0.5],
        #                    [1.0, 1.0, 1.0],
        #                    [0.5, 1.0, 0.5]])
        # for i in xrange(3):
        #     for j in xrange(3):
        #         u = imocc-imin -1+i
        #         v = jmocc-jmin -1+j
        #         valid = (u>=0) & (u<wh) & (v>=0) & (v<ww)
        #         win[ u[valid], v[valid] ] = kernel[i,j]

        # determine log prob increments for swept cells
        def importance(points):
            """assume points in [index,coordinate]"""
            n  = len(points)
            ii = points[:,0] - imin
            jj = points[:,1] - jmin
            valid = (ii>=0) & (ii<wh) & (jj>=0) & (jj<ww)
            logprob = np.zeros(n)
            logprob[valid] = win[ ii[valid], jj[valid] ]
            return logprob

        # OLD CODE
        # # winfree and winocc are map windows containing swept cells
        # winfree = np.zeros((wh, ww), dtype=bool)
        # winfree[imfree-imin, jmfree-jmin] = True  # NOTE: did s/jmax/jmin/g here
        # winocc = np.zeros((wh, ww), dtype=bool)
        # winocc[imocc-imin, jmocc-jmin] = True
        # # determine log prob increments for swept cells
        # def importance(points):
        #     """assume points in [index,coordinate]"""
        #     n  = len(points)
        #     ii = points[:,0] - imin
        #     jj = points[:,1] - jmin
        #     valid = (ii>=0) & (ii<wh) & (jj>=0) & (jj<ww)
        #     free = np.zeros(n, dtype=bool)
        #     free[valid] = winfree[ ii[valid], jj[valid] ]
        #     occ = np.zeros(n, dtype=bool)
        #     occ[valid] = winocc[ ii[valid], jj[valid] ]
        #     # HACK: hardcoded log prob increments
        #     res = np.zeros(n)
        #     res[free] = -1
        #     res[occ]  = +1
        #     #print "importance: %s hits in %s points"%(hits.sum(),n)
        #     return res

        for p in self.filters:
            p.apply_importance(importance)
        
    def resample(self):
        assert self.filters is not None, "no filters were generated yet"
        for p in self.filters:
            p.resample()




        
def test1():
    """simple particle generation"""
    mpm = MapParticleModel(DEFAULT_MAP)
    particles = mpm.sample_points(20)
    for p in particles:
        print p


        
def test2(state=None):
    """path filters generation"""
    if state is None:
        mpm = MapParticleModel(DEFAULT_MAP)
        mpm.gen_paths_pair(N_PARTICLES)
        with open("path_filters.state", 'w') as fh:
            pickle.dump(mpm, fh)
        print "Saved state to path_filters.state"
    else:
        with open(state) as fh:
            mpm = pickle.load(fh)
        #
        l, r, b, t = mpm.pln.x0, mpm.pln.x0+mpm.pln.scale*(mpm.pln.W-1), mpm.pln.y0, mpm.pln.y0+mpm.pln.scale*(mpm.pln.H-1)
        plt.imshow(mpm.pln.occgrid, cmap=cm.gray_r, extent=(l,r,b,t))
        for p in mpm.paths:
            plt.scatter(p[:,0], p[:,1], marker='.', linewidths=0)
        #
        plt.show()


def test3(state="path_filters.state"):
    """sample paths; arguments: [state]"""
    # 1. load paths
    with open(state) as fh:
        print "Loading filter state from", state
        mpm = pickle.load(fh)
    # 2. initial path samples
    print "Initial sampling"
    mpm.sample_paths(FACTOR)
    #
    l, r, b, t = mpm.pln.x0, mpm.pln.x0+mpm.pln.scale*(mpm.pln.W-1), mpm.pln.y0, mpm.pln.y0+mpm.pln.scale*(mpm.pln.H-1)
    plt.subplot(121)
    plt.imshow(mpm.pln.occgrid, cmap=cm.gray_r, extent=(l,r,b,t))
    for p in mpm.paths:
        plt.scatter(p[:,0], p[:,1], marker='.', linewidths=0)
    plt.subplot(122)
    plt.imshow(mpm.pln.occgrid, cmap=cm.gray_r, extent=(l,r,b,t))
    for f in mpm.filters:
        plt.scatter(f.points[:,0], f.points[:,1], marker='.', linewidths=0)
    plt.show()


def test4(state="path_filters.state", scan="scan.state"):
    """one cycle of path particle filter; arguments: [state [scan]]"""
    # 1. load paths
    with open(state) as fh:
        print "Loading filter state from", state
        mpm = pickle.load(fh)
    # 2. initial path samples
    print "Initial sampling"
    mpm.sample_paths(FACTOR)
    # 3. load scan
    with open(scan) as fh:
        data = pickle.load(fh)
        scan = data['scan']
        pose = data['pose']
    # 4. compute importance
    mpm.compute_importance(scan, pose)
    #
    plt.subplot(131)
    img = np.zeros(mpm.pln.occgrid.shape)
    for p in mpm.filters:
        img[p.points[:,0], p.points[:,1]] = 1
    plt.imshow(img, cmap=cm.gray_r)
    #
    plt.subplot(132)
    img = -np.ones(mpm.pln.occgrid.shape)
    for p in mpm.filters:
        img[p.points[:,0], p.points[:,1]] = p.logprob
    plt.imshow(img)
    #
    # 5. resample
    mpm.resample()
    #
    plt.subplot(133)
    img = np.zeros(mpm.pln.occgrid.shape)
    for p in mpm.filters:
        img[p.points[:,0], p.points[:,1]] = 1
    plt.imshow(img, cmap=cm.gray_r)
    #

    
    # 6. diffuse samples


    #
    # plt.subplot(221)
    # plt.imshow(mpm.pln.occgrid, cmap=cm.gray_r)
    #
    # plt.subplot(222)
    # img = np.zeros(mpm.pln.occgrid.shape)
    # (im, jm) = mpm.find_swept_cells(scan, pose)
    # img[im,jm] = 1
    # plt.imshow(img, cmap=cm.gray_r)
    #
    #
    plt.show()


    

def test5(state="scan.state"):
    """extract a scan.state from the first scan found"""
    scan = [None]
    pose = [None]
    def handler(msg):
        if scan[0] is None or pose[0] is None:
            try:
                frame = msg.header.frame_id
                pose[0] = tfl.lookupTransform(MAP_FRAME, frame, rospy.Time())
                scan[0] = msg
                print "got scan from %s to %s"%(MAP_FRAME, frame)
                rospy.signal_shutdown("got scan")
            except tf.Exception:
                print "can't transform from %s to %s, still trying..."%(MAP_FRAME, frame)
    rospy.init_node('test5', anonymous=True, argv=sys.argv)
    tfl = tf.TransformListener()
    sub = rospy.Subscriber("scan", LaserScan, handler)
    print "Waiting for scans"
    rospy.spin()
    if scan[0] is not None and pose[0] is not None:
        with open(state, 'w') as fh:
            state = dict(scan=scan[0], pose=pose[0])
            pickle.dump(state, fh)





def test6(state="path_filters.state"):
    """closed loop experiment"""
    scans = {}
    #
    def lidar_handler(msg):
        frame = msg.header.frame_id
        try:
            pose = tfl.lookupTransform(MAP_FRAME, frame, rospy.Time())
        except tf.Exception:
            return
        scans[frame] = (pose, msg)
    #
    def publish_particles():
        points = []
        logprob = []
        #n = 0
        for pp in mpm.filters:
            (xx, yy) = mpm.pln.np_i2p((pp.points[:,0], pp.points[:,1]))
            points.extend( [Point32(xx[i],yy[i],0) for i in xrange(len(pp.points))] ) # if pp.logprob[i]>=0] )
            logprob.extend(pp.logprob)
            #n += (pp.logprob<0).sum()
        #print "hidding", n, "filters"
        cloud = PointCloud()
        cloud.header.frame_id = MAP_FRAME
        cloud.points = points
        cloud.channels = [ ChannelFloat32(name='logprob', values=logprob) ]
        pub.publish(cloud)
    #
    with open(state) as fh:
        print "Loading filter state from", state
        mpm = pickle.load(fh)
    #
    print "Initial sampling"
    mpm.sample_paths(FACTOR)
    print len(mpm.filters), "filters, total of",
    print sum([len(p.points) for p in mpm.filters]), "particles"
    #
    rospy.init_node('test6', argv=sys.argv)
    tfl = tf.TransformListener()
    sub = rospy.Subscriber("scan", LaserScan, lidar_handler)
    pub = rospy.Publisher("particles", PointCloud, queue_size=1)
    rate = rospy.timer.Rate(1)
    print "Node ready -- starting loop"
    publish_particles()
    while not rospy.is_shutdown():
        for (frame,(pose,scan)) in scans.iteritems():
            print "-- got scan from", frame
            #
            t1 = time.time()
            mpm.compute_importance(scan, pose)
            t2 = time.time()
            #
            print "[compute_importance took %f ms]"%(1000*(t2-t1))
            t1 = time.time()
            mpm.resample()
            t2 = time.time()
            #
            print "[resample took %f ms]"%(1000*(t2-t1))
            publish_particles()
        rate.sleep()



    
def main(argv):
    if len(argv)>1:
        func = globals()[argv[1]]
        args = argv[2:]
        func(*args)
    else:
        print "Missing argument!\nUsage: %s function args"%(argv[0])
        print "Available functions:"
        g = globals()
        l = [f for f in g if f.startswith('test')]
        l.sort()
        for f in l:
            print "  %s -- %s"%(f, g[f].__doc__)


if __name__=='__main__':
    import matplotlib.pyplot as plt
    import matplotlib.cm as cm

    main(rospy.myargv(sys.argv))

# EOF
